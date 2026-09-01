"""Per-message Workspace-mailbox inbox sync (Phase 4, dormant by default).

Reads the connected Workspace mailbox (settings.gmail_delegated_user, via the
existing domain-wide-delegation service account — NO per-user OAuth, NO Google
CASA verification) and stores each message as an EmailMessage with the body
ENCRYPTED at rest, matched to a loan/client where possible. Distinct from the
lender inbound_poller: broader query (in:inbox, not just [QC-] subjects), its own
EmailMessage store + its own dedup namespace, and a body-less breadcrumb Activity
at loan+client level (never the body on a shared surface).

Gated by settings.user_inbox_sync_enabled (default False) AND the same use_fake_inbox
/ gmail_service_account_path / gmail_delegated_user gates the lender poller uses, so
the code ships dormant until Workspace DWD is authorized and the flag is flipped.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.activity import Activity
from app.models.email_message import EmailMessage
from app.models.user import User
from app.services.email import inbound_poller as _poller
from app.services.email.gmail_client import get_gmail_service, gmail_config
from app.services.email.inbox_matcher import match_inbound
from app.services.notifications import notify_inbound_communication

log = logging.getLogger(__name__)

# Broad client/party mail, but EXCLUDE the lender poller's domain ([QC-]-tagged
# threads) so a lender reply isn't ingested by both paths (double breadcrumb + dual
# storage). The lender poller owns tagged mail (Message + email.inbound Activity).
_INBOX_QUERY = 'in:inbox newer_than:14d -subject:"[QC-"'
_BATCH_LIMIT = 40
_TRACKED_KIND = "email.tracked"  # breadcrumb kind — DISTINCT from the lender poller's email.inbound
_sync_lock = asyncio.Lock()


def _encrypt_body(value: str | None) -> tuple[str | None, str]:
    """Encrypt a body string at rest; returns (ciphertext|None, provider).
    Mirrors google_oauth_client._encrypt so bodies use the same Fernet/KMS path."""
    if not value:
        return None, "fernet"
    s = get_settings()
    from app.services.provider_secrets import _encrypt_fernet, _encrypt_kms

    if s.provider_secrets_kms_key_id:
        return _encrypt_kms(value), "aws_kms"
    return _encrypt_fernet(value), "fernet"


def decrypt_body(ciphertext: str | None, provider: str) -> str | None:
    """Decrypt a stored body — used by the inbox read endpoints (owner-only)."""
    if not ciphertext:
        return None
    from app.services.provider_secrets import _decrypt_fernet, _decrypt_kms

    return _decrypt_kms(ciphertext) if provider == "aws_kms" else _decrypt_fernet(ciphertext)


def _headers(detail: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in detail.get("payload", {}).get("headers", [])}


def _bare_addr(raw: str) -> str:
    if "<" in raw and ">" in raw:
        raw = raw.split("<", 1)[1].rsplit(">", 1)[0]
    return raw.strip().lower()


def _addr_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [_bare_addr(part) for part in raw.split(",") if part.strip()]


def _received_at(headers: dict[str, str]) -> datetime | None:
    raw = headers.get("date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:  # noqa: BLE001
        return None


async def _resolve_owner_user_id(db: AsyncSession, mailbox: str) -> uuid.UUID | None:
    from app.enums import Role

    row = (
        await db.execute(select(User).where(func.lower(User.email) == mailbox.lower()))
    ).scalar_one_or_none()
    if row is not None:
        return row.id
    # Fallback: the delegated mailbox is the firm's, owned by the super-admin.
    # Deterministic (earliest-created) so the same mailbox always maps to the same
    # owner across query plans / re-deploys.
    return (
        await db.execute(
            select(User.id)
            .where(User.role == Role.SUPER_ADMIN)
            .order_by(User.created_at.asc(), User.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _processed_ids(db: AsyncSession, mailbox: str) -> set[str]:
    """Gmail ids already stored for this mailbox — dedup within our OWN store
    (separate namespace from the lender poller's email.inbound Activity)."""
    rows = (
        await db.execute(select(EmailMessage.gmail_message_id).where(EmailMessage.mailbox == mailbox))
    ).scalars().all()
    return set(rows)


async def run_user_inbox_sync() -> dict[str, int]:
    """Sync the delegated Workspace mailbox into the EmailMessage store. Dormant
    unless enabled + Gmail DWD configured. Best-effort; never raises to the caller."""
    settings = get_settings()
    if not settings.user_inbox_sync_enabled:
        return {"skipped": 1, "reason_disabled": 1}
    if settings.use_fake_inbox or not settings.gmail_service_account_path or not settings.gmail_delegated_user:
        return {"skipped": 1, "reason_not_configured": 1}
    cfg = gmail_config()
    if cfg is None:
        return {"skipped": 1, "reason_no_cfg": 1}

    async with _sync_lock:
        return await _run_impl(cfg)


async def _run_impl(cfg) -> dict[str, int]:
    from app.db import SessionLocal

    settings = get_settings()
    mailbox = settings.gmail_delegated_user.strip().lower()
    ingested = 0
    skipped = 0

    try:
        svc = await asyncio.to_thread(get_gmail_service, cfg)
    except Exception:  # noqa: BLE001
        log.exception("user_inbox_sync: could not build gmail service")
        return {"error": 1}

    def _list() -> list[dict]:
        resp = svc.users().messages().list(userId="me", q=_INBOX_QUERY, maxResults=_BATCH_LIMIT).execute()
        return resp.get("messages", []) or []

    try:
        refs = await asyncio.to_thread(_list)
    except Exception:  # noqa: BLE001
        log.exception("user_inbox_sync: list failed mailbox=%s", mailbox)
        return {"error": 1}

    async with SessionLocal() as db:
        owner_user_id = await _resolve_owner_user_id(db, mailbox)
        if owner_user_id is None:
            log.warning("user_inbox_sync: no owner user for mailbox=%s — skipping", mailbox)
            return {"skipped": 1, "reason_no_owner": 1}
        seen = await _processed_ids(db, mailbox)

        for ref in refs:
            gmail_id = ref.get("id")
            if not gmail_id or gmail_id in seen:
                continue
            try:
                detail = await asyncio.to_thread(
                    lambda gid=gmail_id: svc.users().messages().get(userId="me", id=gid, format="full").execute()
                )
                await _ingest_message(db, owner_user_id=owner_user_id, mailbox=mailbox, gmail_id=gmail_id, detail=detail)
                await db.commit()
                ingested += 1
            except Exception:  # noqa: BLE001
                await db.rollback()
                log.exception("user_inbox_sync: ingest failed mailbox=%s id=%s", mailbox, gmail_id)
                skipped += 1

    log.info("user_inbox_sync: mailbox=%s ingested=%d skipped=%d", mailbox, ingested, skipped)
    return {"ingested": ingested, "skipped": skipped}


async def _ingest_message(db: AsyncSession, *, owner_user_id, mailbox: str, gmail_id: str, detail: dict) -> None:
    headers = _headers(detail)
    subject = headers.get("subject", "")[:998]
    from_email = _bare_addr(headers.get("from", "")) or None
    to_emails = _addr_list(headers.get("to"))
    cc_emails = _addr_list(headers.get("cc"))
    received_at = _received_at(headers)
    # Do NOT persist Gmail's `snippet` — it's a plaintext excerpt of the body, which
    # would sit unencrypted at rest and undercut the body-encryption guarantee. The
    # owner-only read path (Phase 5) derives a preview from the decrypted body instead.
    snippet = None
    thread_id = detail.get("threadId")

    # Reuse the poller's MIME body extraction, then encrypt at rest.
    body_text = _poller._extract_body(detail.get("payload", {})) or None
    body_text_enc, provider = _encrypt_body(body_text)
    attachments = _poller._walk_attachments(detail.get("payload", {}))

    match = await match_inbound(db, sender=from_email or "", subject=subject)

    row = EmailMessage(
        owner_user_id=owner_user_id,
        mailbox=mailbox,
        gmail_message_id=gmail_id,
        gmail_thread_id=thread_id,
        direction="inbound",
        from_email=from_email,
        to_emails=to_emails or None,
        cc_emails=cc_emails or None,
        subject=subject or None,
        snippet=snippet or None,
        body_text_enc=body_text_enc,
        body_html_enc=None,
        encryption_provider=provider,
        received_at=received_at,
        loan_id=match.loan_id,
        client_id=match.client_id,
        matched_party_role=match.party_role,
        has_attachments=bool(attachments),
    )
    db.add(row)
    await db.flush()
    thread_key = row.gmail_thread_id or str(row.id)
    await notify_inbound_communication(
        db,
        recipient_ids={owner_user_id},
        channel="email",
        sender_label=from_email,
        thread_id=f"email:{thread_key}",
        message_id=str(row.id),
        subject=subject,
    )

    # Body-less breadcrumbs on the SHARED loan/client feeds (isolation rule 2):
    # sender/subject/time only — the body lives solely in the owner's inbox.
    payload = {
        "direction": "inbound",
        "from": from_email,
        "subject": subject or None,
        "received_at": received_at.isoformat() if received_at else None,
        "gmail_thread_id": thread_id,
        "party_role": match.party_role,
    }
    summary = f"Email from {from_email or 'unknown'}: {(subject or '')[:120]}"
    if match.loan_id is not None:
        db.add(Activity(loan_id=match.loan_id, actor_id=None, actor_label="email",
                        kind=_TRACKED_KIND, summary=summary, payload=payload))
    if match.client_id is not None:
        db.add(Activity(client_id=match.client_id, actor_id=None, actor_label="email",
                        kind=_TRACKED_KIND, summary=summary, payload=payload))
