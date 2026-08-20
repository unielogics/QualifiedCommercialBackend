"""The client's room, and the link that reaches it.

Every request a rep makes of a business owner lands in the same place: the
bucket upload room, where they can read what is being asked of them, upload
documents, connect a bank and sign what needs signing. So there is one room per
file and one link to it, reused rather than reminted.

Reusing the link matters more than it looks. If every document request minted a
fresh token, an owner who kept the first email would find it dead by the time
they got round to it, and the third request would arrive with the first two
already invisible. One durable link means the last email is always the right
one, and so is the first.

The passcode is the exception. It is only ever known in plaintext at the moment
it is generated, because only its PBKDF2 hash is stored, so a reused link
cannot have its code recovered later. That is deliberate: a link that reaches
the wrong inbox is a much smaller problem when the code went to a different
channel or was read out loud.

Mechanics are copied from the bucket admin routes rather than reinvented, so
the room a rep opens behaves exactly like the room the desk opens.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.bucket import BucketRequestedDocument, BucketUploadLink

from ..models import DealerBusiness
from . import buckets_link

logger = logging.getLogger(__name__)

__all__ = ["ClientRoom", "ensure_room", "request_document", "room_url"]

# Matches the bucket admin routes exactly. Changing either without the other
# would leave clients unable to open rooms created by the other surface.
_PBKDF2_ITERATIONS = 240_000


def _hash_passcode(passcode: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", passcode.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def _generate_passcode() -> str:
    return f"QC-{secrets.randbelow(900000) + 100000}"


def room_url(token: str) -> str:
    """The client-facing URL for a room token.

    The room is served by the main app, not by audit., because it predates
    Capital OS and every existing client link already points there.
    """
    settings = get_settings()
    base = (getattr(settings, "frontend_app_url", "") or "").rstrip("/")
    if settings.app_env.lower() == "production" and (
        not base or "localhost" in base or "127.0.0.1" in base or base.startswith("http://")
    ):
        base = "https://app.qualifiedcommercial.com"
    return f"{base}/buckets/request/{token}"


@dataclass
class ClientRoom:
    link: BucketUploadLink
    url: str
    # Plaintext only when this call created the link. None for a reused one,
    # because the stored hash cannot be reversed. Callers must treat None as
    # "the client already has their code" rather than as an error.
    passcode: str | None


async def ensure_room(db: AsyncSession, dealer: DealerBusiness) -> ClientRoom:
    """The file's client room, created if it does not exist yet. Flushes, never commits."""
    bucket = await buckets_link.ensure_bucket(db, dealer)

    link = (
        await db.execute(
            select(BucketUploadLink)
            .where(
                BucketUploadLink.bucket_id == bucket.id,
                BucketUploadLink.status == "active",
            )
            .order_by(BucketUploadLink.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if link is not None:
        return ClientRoom(link, room_url(link.token), None)

    passcode = _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=(dealer.name or "Business owner")[:180],
        recipient_email=(dealer.email or None),
        passcode_hash=_hash_passcode(passcode),
    )
    db.add(link)
    await db.flush()
    logger.info("dealer-os: opened client room %s for dealer %s", link.id, dealer.id)
    return ClientRoom(link, room_url(link.token), passcode)


async def request_document(
    db: AsyncSession,
    dealer: DealerBusiness,
    *,
    name: str,
    description: str | None = None,
    category: str | None = None,
    requires_signature: bool = False,
    signature_kind: str | None = None,
    required: bool = True,
) -> BucketRequestedDocument:
    """Put an item on the client's checklist. Flushes, never commits.

    Signature requests are the same object with `requires_signature` set, which
    is what the existing signing flow keys off. Modelling them as a separate
    thing would mean two checklists for the client to work through and two
    places to notice that something is still outstanding.
    """
    bucket = await buckets_link.ensure_bucket(db, dealer)

    # An identical open request is a duplicate, not a second ask. Reps re-send
    # reminders, and a checklist that grows a new row each time is one the
    # client stops reading.
    existing = (
        await db.execute(
            select(BucketRequestedDocument).where(
                BucketRequestedDocument.bucket_id == bucket.id,
                BucketRequestedDocument.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if description and not existing.description:
            existing.description = description
        await db.flush()
        return existing

    doc = BucketRequestedDocument(
        bucket_id=bucket.id,
        name=name,
        category=category,
        description=description,
        required=required,
        is_custom=True,
        requires_signature=requires_signature,
        signature_kind=signature_kind,
    )
    db.add(doc)
    await db.flush()
    return doc
