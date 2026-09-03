"""Signatures on file — request/response shapes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class StoredSignatureRead(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID | None = None
    typed_name: str
    title: str | None = None
    source: str
    adopted_at: datetime
    adopted_by_user_id: UUID | None = None
    consent_version: str | None = None
    revoked_at: datetime | None = None
    # Presigned GET of the stored image (15 minutes); only when asked for.
    preview_url: str | None = None


class StoredSignatureAdoptBody(BaseModel):
    signature_data_url: str = Field(min_length=1)
    typed_name: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    consent: bool = False


class StoredSignatureState(BaseModel):
    """What the profile page shows: the live signature (or null) and the
    consent the user accepts when adopting one."""

    signature: StoredSignatureRead | None = None
    consent_text: str
    consent_version: str
