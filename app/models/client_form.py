"""Client-filled forms that are not financial statements (the real-estate loan
application first), and their frozen versions.

``client_forms`` keeps one live row per file and form kind: only what someone
actually typed (``answers``) and who touched each field (``touched``). Every
read merges live prefill underneath, so a later desk correction reaches every
field nobody typed. A submission freezes the merged body into an immutable
``client_form_versions`` row; signing works on that frozen copy only.

Both tables carry ``profile_id`` so the application-history retention triggers
(migration 0239) protect them like the rest of the file's history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClientForm(Base):
    __tablename__ = "client_forms"
    __table_args__ = (
        UniqueConstraint("profile_id", "kind", name="uq_client_forms_profile_kind"),
        CheckConstraint("kind in ('re_application')", name="ck_client_forms_kind"),
        CheckConstraint("status in ('draft','submitted')", name="ck_client_forms_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    touched: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("client_form_versions.id", ondelete="SET NULL", use_alter=True, name="fk_client_forms_current_version"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ClientFormVersion(Base):
    __tablename__ = "client_form_versions"
    __table_args__ = (
        UniqueConstraint("form_id", "number", name="uq_client_form_versions_number"),
        UniqueConstraint("form_id", "idempotency_key", name="uq_client_form_versions_idempotency"),
        CheckConstraint("submitted_by in ('client','desk')", name="ck_client_form_versions_submitted_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    form_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("client_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_s3_key: Mapped[str | None] = mapped_column(String(512))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    submitted_by: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
