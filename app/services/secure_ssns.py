from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from contextlib import closing
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.config import get_settings
from app.dealer_os.services.storage import _s3_client
from app.models.application_profile import ApplicationProfile
from app.models.secure_ssn import SecureSsn, SecureSsnAccess
from app.services import arf_channel

MAX_PEOPLE = 25


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID4
    person_name: str = Field(min_length=1, max_length=120)
    ssn: SecretStr

    @field_validator("person_name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        name = unicodedata.normalize("NFC", " ".join(name.split()))
        if not any(ch.isalpha() for ch in name) or not all(ch.isalpha() or ch in " .,'’-‐‑-" for ch in name):
            raise ValueError("Enter the person's name")
        return name

    @field_validator("ssn")
    @classmethod
    def validate_ssn(cls, value: SecretStr) -> SecretStr:
        digits = value.get_secret_value()
        if not re.fullmatch(r"[0-9]{9}|[0-9]{3}-[0-9]{2}-[0-9]{4}", digits):
            raise ValueError("Enter a nine-digit Social Security number")
        return SecretStr(digits.replace("-", ""))


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[Entry] = Field(default_factory=list, max_length=MAX_PEOPLE)
    authorized: StrictBool = False


class Receipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    person_name: str
    last_four: str


def configured() -> bool:
    settings = get_settings()
    return bool(settings.s3_bucket and settings.buckets_kms_key_id)


def _file_cipher(profile_id: UUID, *, create: bool) -> AESGCM:
    """One random file key held in private SSE-KMS storage, never in Postgres."""
    if not configured():
        raise HTTPException(503, "Secure SSN storage is temporarily unavailable.")
    settings = get_settings()
    for name in ("botocore.endpoint", "botocore.parsers"):
        logger = logging.getLogger(name)
        if logger.isEnabledFor(logging.DEBUG):
            logger.setLevel(logging.INFO)
    obj = {"Bucket": settings.s3_bucket, "Key": f"private-identifiers/keys/v1/{profile_id}"}
    try:
        s3 = _s3_client()
        try:
            stored = s3.get_object(**obj)
        except ClientError as exc:
            if not create or exc.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                raise
            try:
                s3.put_object(**obj, Body=AESGCM.generate_key(bit_length=256), IfNoneMatch="*",
                              ServerSideEncryption="aws:kms", SSEKMSKeyId=settings.buckets_kms_key_id)
            except ClientError as race:
                if race.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                    raise
            stored = s3.get_object(**obj)
        with closing(stored["Body"]) as stream:
            if stored.get("ServerSideEncryption") != "aws:kms" or not stored.get("SSEKMSKeyId"):
                raise ValueError("Encryption confirmation missing")
            key = stream.read(33)
            if len(key) != 32:
                raise ValueError("File key unavailable")
            return AESGCM(key)
    except (BotoCoreError, ClientError, ValueError, OSError):
        raise HTTPException(503, "Secure SSN storage is temporarily unavailable.") from None


def _context(profile_id: UUID, entry_id: UUID) -> bytes:
    return b"qc:ssn:v1:" + profile_id.bytes + entry_id.bytes


async def receipts(db, profile_id: UUID) -> list[Receipt]:
    records = (await db.scalars(
        select(SecureSsn).where(SecureSsn.profile_id == profile_id).order_by(SecureSsn.created_at, SecureSsn.id)
    )).all()
    return [Receipt.model_validate(row) for row in records]


async def client_entries(db, profile, submission: Submission) -> list[Receipt]:
    await arf_channel.require_profile_hosted_permission(db, profile, requested_fields=["full_ssn"])
    if not configured():
        raise HTTPException(503, "Secure SSN storage is temporarily unavailable.")
    if not submission.entries:
        return await receipts(db, profile.id)
    if not submission.authorized:
        raise HTTPException(422, "Confirm that you may share each person's SSN for this application.")
    ids = {entry.id for entry in submission.entries}
    if len(ids) != len(submission.entries):
        raise HTTPException(422, "Each person needs a separate entry.")
    # Serialize the per-file limit and writes, including retries from other tabs.
    await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    saved = await receipts(db, profile.id)
    if ids.intersection(row.id for row in saved):
        raise HTTPException(409, "A submitted entry is already on file. Check your saved entries.")
    if len(saved) + len(ids) > MAX_PEOPLE:
        raise HTTPException(422, f"A file can hold up to {MAX_PEOPLE} people.")
    cipher = await asyncio.to_thread(_file_cipher, profile.id, create=not saved)
    rows = []
    for entry in submission.entries:
        digits = entry.ssn.get_secret_value()
        nonce = os.urandom(12)
        ciphertext = nonce + cipher.encrypt(nonce, digits.encode("ascii"), _context(profile.id, entry.id))
        rows.append(SecureSsn(profile_id=profile.id, id=entry.id, person_name=entry.person_name,
                              last_four=digits[-4:], ciphertext=ciphertext))
    db.add_all(rows)
    await db.commit()
    return [*saved, *(Receipt.model_validate(row) for row in rows)]


async def reveal(db, profile_id: UUID, entry_id: UUID, user_id: UUID) -> str:
    row = await db.scalar(select(SecureSsn).options(undefer(SecureSsn.ciphertext)).where(
        SecureSsn.profile_id == profile_id, SecureSsn.id == entry_id
    ))
    if row is None:
        raise HTTPException(404, "SSN entry not found.")
    cipher = await asyncio.to_thread(_file_cipher, profile_id, create=False)
    try:
        plaintext = cipher.decrypt(row.ciphertext[:12], row.ciphertext[12:], _context(profile_id, entry_id))
    except (InvalidTag, ValueError):
        raise HTTPException(503, "This SSN could not be verified.") from None
    db.add(SecureSsnAccess(profile_id=profile_id, entry_id=entry_id, user_id=user_id))
    await db.commit()
    return plaintext.decode("ascii")
