"""Correlate shared Dealer Desk replies to a prospect email draft.

The primary key is the unguessable plus-address token in Reply-To.  A stable
RFC Message-ID on the outbound MIME message is the fallback for mail systems
that rewrite or omit the original recipient header.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.dealer_prospect import DealerProspect, DealerProspectActivity
from app.models.prospect_outreach import (
    DealerProspectEmailDraft,
    DealerProspectInboundReply,
)
from app.services.email.user_inbox_sync import _encrypt_body
from app.services.notifications import notify_users
from app.services.prospect_outreach import normalize_email, token_hash


@dataclass(frozen=True)
class ReplyMatch:
    draft_id: uuid.UUID
    prospect_id: uuid.UUID


@dataclass(frozen=True)
class ReplyIngestResult:
    matched: bool
    duplicate: bool = False
    prospect_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None


def _message_ids(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    combined = " ".join(value) if not isinstance(value, str) else value
    # Preserve angle brackets because the draft stores the canonical header.
    return [item.strip() for item in re.findall(r"<[^<>\s]+>", combined) if item.strip()]


def reply_tokens(addresses: Iterable[str]) -> list[str]:
    settings = get_settings()
    base = normalize_email(settings.prospect_reply_to_email)
    base_local, _, base_domain = base.partition("@")
    base_local = base_local.split("+", 1)[0]
    tokens: list[str] = []
    for _, address in getaddresses([(str(value)) for value in addresses]):
        local, separator, domain = normalize_email(address).partition("@")
        if not separator or domain != base_domain or not local.startswith(f"{base_local}+"):
            continue
        token = local[len(base_local) + 1 :]
        if re.fullmatch(r"[A-Za-z0-9_-]{12,80}", token):
            tokens.append(token)
    return list(dict.fromkeys(tokens))


async def match_reply(
    db: AsyncSession,
    *,
    to_addresses: Iterable[str],
    in_reply_to: str | None = None,
    references: Iterable[str] | None = None,
) -> ReplyMatch | None:
    tokens = reply_tokens(to_addresses)
    if tokens:
        hashes = [token_hash(token) for token in tokens]
        row = (
            (
                await db.execute(
                    select(DealerProspectEmailDraft).where(
                        DealerProspectEmailDraft.reply_token_hash.in_(hashes)
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return ReplyMatch(draft_id=row.id, prospect_id=row.prospect_id)

    ids = _message_ids(in_reply_to) + _message_ids(references)
    if ids:
        row = (
            (
                await db.execute(
                    select(DealerProspectEmailDraft)
                    .where(DealerProspectEmailDraft.rfc_message_id.in_(list(dict.fromkeys(ids))))
                    .order_by(DealerProspectEmailDraft.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return ReplyMatch(draft_id=row.id, prospect_id=row.prospect_id)
    return None


async def ingest_reply(
    db: AsyncSession,
    *,
    provider: str,
    provider_message_id: str,
    from_email: str,
    to_addresses: list[str],
    subject: str | None,
    body: str | None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    received_at: datetime | None = None,
) -> ReplyIngestResult:
    existing = (
        await db.execute(
            select(DealerProspectInboundReply).where(
                DealerProspectInboundReply.provider == provider[:24],
                DealerProspectInboundReply.provider_message_id == provider_message_id[:320],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ReplyIngestResult(
            matched=True,
            duplicate=True,
            prospect_id=existing.prospect_id,
            draft_id=existing.draft_id,
        )

    match = await match_reply(
        db,
        to_addresses=to_addresses,
        in_reply_to=in_reply_to,
        references=references,
    )
    if match is None:
        return ReplyIngestResult(matched=False)
    prospect = await db.get(DealerProspect, match.prospect_id)
    if prospect is None:
        return ReplyIngestResult(matched=False)
    encrypted, encryption_provider = _encrypt_body(body)
    row = DealerProspectInboundReply(
        prospect_id=match.prospect_id,
        draft_id=match.draft_id,
        provider=provider[:24],
        provider_message_id=provider_message_id[:320],
        from_email=normalize_email(from_email),
        to_emails=[normalize_email(value) for value in to_addresses if normalize_email(value)],
        subject=(subject or "")[:998] or None,
        body_text_enc=encrypted,
        encryption_provider=encryption_provider,
        in_reply_to=(in_reply_to or "")[:500] or None,
        references=_message_ids(references),
        received_at=received_at or datetime.now(UTC),
    )
    db.add(row)
    db.add(
        DealerProspectActivity(
            prospect_id=match.prospect_id,
            actor_user_id=None,
            kind="email.reply_received",
            body=f"Reply received from {normalize_email(from_email) or 'unknown sender'}",
            metadata_json={
                "reply_id": str(row.id),
                "draft_id": str(match.draft_id),
                "provider": provider[:24],
                "provider_message_id": provider_message_id[:320],
                "subject": (subject or "")[:240] or None,
            },
        )
    )
    prospect.last_activity_at = received_at or datetime.now(UTC)
    # Let the provider-id unique constraint propagate if a concurrent ingest
    # wins.  The mailbox sync owns the outer rollback/retry; swallowing an
    # IntegrityError here would leave its AsyncSession unusable.
    await db.flush()

    if prospect.owner_user_id:
        await notify_users(
            db,
            recipient_ids={prospect.owner_user_id},
            event_type="dealer_prospect_email_reply",
            category="messages",
            priority="high",
            title="Dealer prospect replied",
            body=(subject or "Open the prospect to review the reply.")[:240],
            target_type="dealer_prospect",
            target_id=str(prospect.id),
            deep_link=f"/marketing/prospects/{prospect.id}",
            meta={"prospect_id": str(prospect.id), "draft_id": str(match.draft_id)},
            email=False,
            push=True,
        )
    return ReplyIngestResult(
        matched=True,
        prospect_id=match.prospect_id,
        draft_id=match.draft_id,
    )


async def ingest_synced_reply(
    db: AsyncSession,
    *,
    from_email: str | None,
    to_addresses: list[str],
    subject: str,
    body: str | None,
    gmail_id: str,
    headers: dict[str, str],
    received_at: datetime | None,
) -> ReplyIngestResult:
    """Narrow hook used by the existing Workspace inbox synchronizer."""
    if not from_email:
        return ReplyIngestResult(matched=False)
    return await ingest_reply(
        db,
        provider="gmail",
        provider_message_id=gmail_id,
        from_email=from_email,
        to_addresses=to_addresses,
        subject=subject,
        body=body,
        in_reply_to=headers.get("in-reply-to"),
        references=_message_ids(headers.get("references")),
        received_at=received_at,
    )
