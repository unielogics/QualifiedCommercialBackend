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


def _is_permission_failure(exc: Exception) -> bool:
    """Is this Google telling us the token may not send, rather than failing?

    Matched on the HTTP status where the client exposes one and on the reason
    string otherwise, because googleapiclient raises HttpError for everything
    from a 403 to a gateway timeout and only the former means "reconnect".
    """
    status_code = getattr(getattr(exc, "resp", None), "status", None)
    if status_code in (401, 403):
        return True
    text = str(exc)
    return "insufficientPermissions" in text or "insufficient authentication scopes" in text


async def _forget_gmail_send_scope(db: AsyncSession, sender_user_id: uuid.UUID) -> None:
    """Drop gmail.send from the stored grant so we stop trying it.

    Deliberately narrow: the account stays connected and its other scopes keep
    working. Reconnecting Google restores this one.
    """
    account = await google_oauth_client.get_account(db, sender_user_id)
    if account is None or not account.scopes:
        return
    remaining = [scope for scope in account.scopes if scope not in GMAIL_SCOPES]
    if len(remaining) != len(account.scopes):
        account.scopes = remaining
        await db.flush()


async def _try_gmail_send_as_user(
    db: AsyncSession,
    sender_user_id: uuid.UUID,
    *,
    to_emails: list[str],
    subject: str,
    body_text: str,
    body_html: str | None,
    cc_emails: list[str] | None,
    bcc_emails: list[str] | None,
    attachments: list[tuple[str, bytes, str]] | None,
    headers: dict[str, str] | None,
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

    def _send() -> tuple[str | None, str | None]:
        from googleapiclient.discovery import build

        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        built = gmail_client.build_message(
            to=", ".join(to_emails),
            subject=subject,
            body=body_text,
            body_html=body_html,
            from_email=from_email,
            cc=cc_emails,
            bcc=bcc_emails,
            attachments=gmail_attachments,
            headers=headers,
        )
        resp = svc.users().messages().send(userId="me", body={"raw": built.raw_base64}).execute()
        return resp.get("id"), resp.get("threadId")

    try:
        import asyncio

        msg_id, thread_id = await asyncio.to_thread(_send)
        log.info("send_as_user: sent via Gmail user=%s to=%s id=%s", sender_user_id, to_emails, msg_id)
        return SesSendResult(True, msg_id, "sent_gmail", thread_id)
    except Exception as exc:  # noqa: BLE001
        if _is_permission_failure(exc):
            # Google says this token cannot send, while our own record of the
            # granted scopes says it can. That is the GoogleScopeMissing case
            # arriving from the other direction, and the three conditions that
            # raise it all fall back to SES — so this does too, and the email
            # goes out instead of dying with a 403 in a detail string.
            #
            # The stale scope is corrected on the way past, so the next send
            # skips Gmail entirely and anything reading the account's scopes
            # can prompt a reconnect rather than repeating this round trip.
            log.warning(
                "send_as_user: Gmail refused the send for user=%s (%s); "
                "clearing the stale gmail.send scope and falling back to SES",
                sender_user_id, exc,
            )
            try:
                await _forget_gmail_send_scope(db, sender_user_id)
            except Exception:  # noqa: BLE001
                log.exception("send_as_user: could not clear stale scope user=%s", sender_user_id)
            return None
        # A genuine transport failure — a timeout, a 5xx. Surfaced rather than
        # silently re-sent through SES, because a retry may well duplicate it.
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
    headers: dict[str, str] | None = None,
) -> SesSendResult:
    """Send from the sender's connected Gmail when available, else firm SES.

    - sender_user_id None → straight to SES.
    - Gmail connected → send via their Gmail (bcc and multipart HTML supported).
    - Not connected → SES send_raw_email (cc + attachments + html supported; no bcc).
    """
    if sender_user_id is not None:
        gmail_result = await _try_gmail_send_as_user(
            db,
            sender_user_id,
            to_emails=to_emails,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc_emails=cc_emails,
            bcc_emails=bcc_emails,
            attachments=attachments,
            headers=headers,
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
        headers=headers,
    )
