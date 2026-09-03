"""Signatures on file: adopt, revoke and resolve the one live signature per
subject (team member, sponsor company, Qualified Commercial) that gets
placed on program agreements on that subject's behalf.

The dealer is deliberately absent: the client signs fresh every time.

Storage: a user's pad drawing goes to
``stored-signatures/users/{user_id}/{sha16}.png``; an admin-uploaded company
signature to ``stored-signatures/companies/{company_id}/{sha16}.png``; a
sponsor's Referral Protection Agreement signature and the firm's letterhead
signature are referenced where they already live (no re-upload). Every row
carries the sha256 of the image so placement can verify what it fetched.

Routes own the transaction; this module flushes. Storage outages surface
as 503 ``storage_unavailable`` the way the production package does.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.services import storage
from app.models.contract_agreement import ContractAgreement
from app.models.stored_signature import (
    STORED_SIGNATURE_SOURCES,
    STORED_SIGNATURE_SUBJECT_TYPES,
    StoredSignature,
)
from app.services.payment_authorization import (
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
)

logger = logging.getLogger(__name__)

SUBJECT_TYPES: tuple[str, ...] = STORED_SIGNATURE_SUBJECT_TYPES
SOURCES: tuple[str, ...] = STORED_SIGNATURE_SOURCES

STORED_SIGNATURE_CONSENT_VERSION = "2026-09-03-1"
STORED_SIGNATURE_CONSENT_TEXT = (
    "I adopt this signature for use on my behalf on Qualified Commercial program agreements where I am named as "
    "the relationship manager or an acknowledging party. I understand that Qualified Commercial will place it "
    "electronically, on my behalf and without a separate signing step, each time such an agreement is sent, that "
    "each placement is recorded with the date I adopted this signature and this consent version, and that my "
    "electronic signature has the same effect as a handwritten one under the U.S. E-SIGN Act and UETA. I may revoke "
    "this adoption at any time; revocation does not affect agreements already sent."
)
COMPANY_SIGNATURE_AUTHORIZATION_VERSION = "2026-09-03-1"
COMPANY_SIGNATURE_AUTHORIZATION_TEXT = (
    "I confirm that the company named has authorized Qualified Commercial LLC to place this officer's signature on "
    "its behalf on Qualified Commercial program agreements where the company is named as sponsor, that the signer "
    "and title recorded here are the officer who provided it, that I hold or have reviewed that authorization, and "
    "that I am recording it on behalf of Qualified Commercial LLC."
)
PREVIEW_TTL_SECONDS = 900

_USER_KEY = "stored-signatures/users/{subject_id}/{sha16}.png"
_COMPANY_KEY = "stored-signatures/companies/{subject_id}/{sha16}.png"


def _now() -> datetime:
    return datetime.now(UTC)


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "storage_unavailable", "message": "Signature storage is not configured; nothing was saved."},
    )


def _unprocessable(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(422, detail={"code": code, "message": message, **extra})


def _evidence(request: Request | None) -> tuple[str | None, str | None]:
    ua = (request.headers.get("user-agent", "") if request is not None else "")[:400] or None
    return client_ip(request), ua


def _clean(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())
    return text[:limit] or None


def _decode_png(signature_data_url: str | None) -> tuple[bytes, str]:
    try:
        png, sha, content_type = decode_signature_data_url(signature_data_url)
    except Exception as exc:  # noqa: BLE001 — bad base64 / data URL
        raise _unprocessable("signature_invalid", "The signature image could not be read.") from exc
    if not png or not sha:
        raise _unprocessable("signature_required", "Draw your signature to continue.")
    if not content_type.startswith("image/"):
        raise _unprocessable("signature_invalid", "The signature must be an image.")
    return png, sha


def _check_subject(subject_type: str, subject_id: UUID | None) -> None:
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"Unknown stored-signature subject type: {subject_type}")
    if subject_type == "qc" and subject_id is not None:
        raise ValueError("Qualified Commercial's signature has no subject id")
    if subject_type != "qc" and subject_id is None:
        raise ValueError(f"A {subject_type} signature needs a subject id")


# ---- resolve ----------------------------------------------------------------

def _live_query(subject_type: str, subject_id: UUID | None):
    q = select(StoredSignature).where(StoredSignature.subject_type == subject_type, StoredSignature.revoked_at.is_(None))
    if subject_id is None:
        return q.where(StoredSignature.subject_id.is_(None))
    return q.where(StoredSignature.subject_id == subject_id)


async def current(db: AsyncSession, subject_type: str, subject_id: UUID | None) -> StoredSignature | None:
    """The one live signature for the subject, or None."""
    _check_subject(subject_type, subject_id)
    return (await db.execute(_live_query(subject_type, subject_id))).scalar_one_or_none()


async def current_user_signature(db: AsyncSession, user_id: UUID) -> StoredSignature | None:
    return await current(db, "user", user_id)


async def current_company_signature(db: AsyncSession, company_id: UUID) -> StoredSignature | None:
    return await current(db, "company", company_id)


async def current_qc_signature(db: AsyncSession) -> StoredSignature | None:
    return await current(db, "qc", None)


# ---- adopt / revoke ---------------------------------------------------------

async def _retire_live(db: AsyncSession, *, subject_type: str, subject_id: UUID | None, by_user_id: UUID | None,
                       now: datetime) -> StoredSignature | None:
    """Revoke the live row (if any) and flush, so the partial unique index
    admits the replacement. The unit of work would otherwise INSERT before
    it UPDATEs."""
    previous = await current(db, subject_type, subject_id)
    if previous is not None:
        previous.revoked_at = now
        previous.revoked_by_user_id = by_user_id
        await db.flush()
    return previous


async def _adopt(
    db: AsyncSession, *, subject_type: str, subject_id: UUID | None, typed_name: str, title: str | None,
    signature_s3_key: str, signature_sha256: str, source: str, adopted_by_user_id: UUID | None,
    consent_version: str | None, authorization_note: str | None, source_agreement_id: UUID | None,
    request: Request | None,
) -> StoredSignature:
    _check_subject(subject_type, subject_id)
    if source not in SOURCES:
        raise ValueError(f"Unknown stored-signature source: {source}")
    name = _clean(typed_name, 160)
    if not name:
        raise _unprocessable("typed_name_required", "Type the signer's name as it should appear.")
    now = _now()
    ip, ua = _evidence(request)
    await _retire_live(db, subject_type=subject_type, subject_id=subject_id, by_user_id=adopted_by_user_id, now=now)
    row = StoredSignature(
        subject_type=subject_type, subject_id=subject_id, typed_name=name, title=_clean(title, 120),
        signature_s3_key=signature_s3_key, signature_sha256=signature_sha256, source=source,
        source_agreement_id=source_agreement_id, adoption_consent_version=consent_version, adopted_at=now,
        adopted_by_user_id=adopted_by_user_id, adopted_ip=ip, adopted_user_agent=ua,
        authorization_note=_clean(authorization_note, 4000),
    )
    db.add(row)
    await db.flush()
    logger.info("stored-signature adopted subject=%s/%s source=%s by=%s", subject_type, subject_id, source, adopted_by_user_id)
    return row


async def adopt_user_signature(
    db: AsyncSession, *, user: Any, signature_data_url: str | None, typed_name: str | None, title: str | None = None,
    consent: bool, request: Request | None,
) -> StoredSignature:
    """A team member / relationship manager adopts their own pad drawing
    (E-SIGN adoption consent required). Replaces any previous live row."""
    if not consent:
        raise _unprocessable("consent_required", "Accept the signature adoption consent to continue.")
    png, sha = _decode_png(signature_data_url)
    key = _USER_KEY.format(subject_id=user.id, sha16=sha[:16])
    if not storage.put_bytes(key, png, "image/png"):
        raise _storage_unavailable()
    return await _adopt(
        db, subject_type="user", subject_id=user.id, typed_name=typed_name or getattr(user, "name", None), title=title,
        signature_s3_key=key, signature_sha256=sha, source="self_adopted", adopted_by_user_id=user.id,
        consent_version=STORED_SIGNATURE_CONSENT_VERSION, authorization_note=None, source_agreement_id=None,
        request=request,
    )


async def revoke(
    db: AsyncSession, *, subject_type: str, subject_id: UUID | None, user: Any, reason: str | None = None,
) -> StoredSignature | None:
    """Retire the live signature. Documents it was already placed on are
    untouched (placements are immutable evidence). None when there was
    nothing live."""
    now = _now()
    previous = await _retire_live(db, subject_type=subject_type, subject_id=subject_id, by_user_id=user.id, now=now)
    if previous is not None:
        logger.info("stored-signature revoked id=%s subject=%s/%s by=%s reason=%s",
                    previous.id, subject_type, subject_id, user.id, (reason or "").strip()[:200] or "-")
    return previous


async def adopt_company_signature_from_agreement(
    db: AsyncSession, *, company_id: UUID, agreement: ContractAgreement | Any, admin: Any,
    authorization_note: str | None, request: Request | None,
) -> StoredSignature:
    """A super admin authorizes the officer signature the sponsor company
    already gave on its Referral Protection Agreement. The image is
    referenced where it lives — no re-upload."""
    if agreement is None or getattr(agreement, "subject_type", None) != "company" or agreement.subject_id != company_id:
        raise _unprocessable("agreement_mismatch", "That agreement was not signed by this company.")
    if not agreement.signature_s3_key or not agreement.signature_hash:
        raise _unprocessable("agreement_signature_missing", "The agreement carries no drawn signature to adopt.")
    values = agreement.field_values or {}
    title = values.get("counterparty_signatory_title") or values.get("signatory_title")
    return await _adopt(
        db, subject_type="company", subject_id=company_id, typed_name=agreement.typed_name, title=title,
        signature_s3_key=agreement.signature_s3_key, signature_sha256=agreement.signature_hash, source="agreement",
        adopted_by_user_id=admin.id, consent_version=COMPANY_SIGNATURE_AUTHORIZATION_VERSION,
        authorization_note=authorization_note, source_agreement_id=agreement.id, request=request,
    )


async def adopt_company_signature_upload(
    db: AsyncSession, *, company_id: UUID, admin: Any, signature_data_url: str | None, typed_name: str | None,
    title: str | None, authorization_note: str | None, request: Request | None,
) -> StoredSignature:
    """A super admin records an authorized officer signature for a sponsor
    company from an image the company provided."""
    png, sha = _decode_png(signature_data_url)
    key = _COMPANY_KEY.format(subject_id=company_id, sha16=sha[:16])
    if not storage.put_bytes(key, png, "image/png"):
        raise _storage_unavailable()
    return await _adopt(
        db, subject_type="company", subject_id=company_id, typed_name=typed_name, title=title,
        signature_s3_key=key, signature_sha256=sha, source="admin_recorded", adopted_by_user_id=admin.id,
        consent_version=COMPANY_SIGNATURE_AUTHORIZATION_VERSION, authorization_note=authorization_note,
        source_agreement_id=None, request=request,
    )


async def adopt_qc_signature(
    db: AsyncSession, *, admin: Any, signature_s3_key: str | None, signature_sha256: str | None, typed_name: str | None,
    title: str | None, request: Request | None,
) -> StoredSignature:
    """Qualified Commercial's own signature: the letterhead signature image
    (``app.routers.settings.SIGNATURE_S3_KEY`` = firm_settings/letterhead_signature.png,
    saved on ``letterhead.signature_s3_key``) plus the signer name/title.
    The sha is computed from storage when the caller does not have it."""
    if not signature_s3_key:
        raise _unprocessable("letterhead_signature_missing", "Upload the company signature image in Settings first.")
    sha = signature_sha256
    if not sha:
        raw = storage.get_bytes(signature_s3_key)
        if raw is None:
            raise _storage_unavailable()
        sha = hashlib.sha256(raw).hexdigest()
    return await _adopt(
        db, subject_type="qc", subject_id=None, typed_name=typed_name, title=title,
        signature_s3_key=signature_s3_key, signature_sha256=sha, source="letterhead", adopted_by_user_id=admin.id,
        consent_version=COMPANY_SIGNATURE_AUTHORIZATION_VERSION, authorization_note=None, source_agreement_id=None,
        request=request,
    )


# ---- read -------------------------------------------------------------------

def signature_png(sig: StoredSignature | None) -> bytes | None:
    """The stored image bytes, verified against the recorded sha256. None
    when storage is unavailable or the object does not match its record."""
    if sig is None or not sig.signature_s3_key:
        return None
    raw = storage.get_bytes(sig.signature_s3_key)
    if raw is None:
        return None
    if sig.signature_sha256 and hashlib.sha256(raw).hexdigest() != sig.signature_sha256:
        logger.warning("stored-signature %s: object %s does not match its recorded sha256", sig.id, sig.signature_s3_key)
        return None
    return raw


def read_model(sig: StoredSignature | None, *, presign: bool = False) -> dict[str, Any] | None:
    if sig is None:
        return None
    return {
        "id": sig.id,
        "subject_type": sig.subject_type,
        "subject_id": sig.subject_id,
        "typed_name": sig.typed_name,
        "title": sig.title,
        "source": sig.source,
        "adopted_at": sig.adopted_at,
        "adopted_by_user_id": sig.adopted_by_user_id,
        "consent_version": sig.adoption_consent_version,
        "revoked_at": sig.revoked_at,
        "preview_url": presign_private_s3_object(sig.signature_s3_key, ttl_seconds=PREVIEW_TTL_SECONDS) if presign else None,
    }
