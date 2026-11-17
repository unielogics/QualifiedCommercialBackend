"""A signing pass over one frozen client-form version.

One session per submitted version while it is being signed. Each signer's
signature is stamped onto the previous output (the chain records every input
and output hash), and when the last required signer signs the executed copy,
with a certificate page per signer, is filed on the requirement's checklist row.
A desk "Reopen" voids the session (never deletes it) so the client can edit
and submit a new version.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FormSigningSession(Base):
    __tablename__ = "form_signing_sessions"
    __table_args__ = (
        CheckConstraint("status in ('in_progress','executed','void')", name="ck_form_signing_sessions_status"),
        # A version has at most one signing pass that still counts (migration 0282).
        Index(
            "uq_form_signing_sessions_live_version", "version_id", unique=True,
            postgresql_where=text("status in ('in_progress','executed')"),
            sqlite_where=text("status in ('in_progress','executed')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    form_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("client_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("client_form_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_document_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress", server_default="in_progress")
    #: The merged signature settings and the consents (key, version, sha256) as signed.
    signature_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    required_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: role → {name, email, signature_id, signed_at, link_id}
    signers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    unsigned_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_pdf_key: Mapped[str | None] = mapped_column(String(512))
    current_sha256: Mapped[str | None] = mapped_column(String(64))
    #: [{role, signature_id, input_sha256, output_sha256, at}]
    chain: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    executed_file_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(500))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
