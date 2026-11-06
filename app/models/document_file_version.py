"""Immutable uploaded binaries for a stable legacy loan-document identity."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentFileVersion(Base):
    __tablename__ = "document_file_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_file_version_number"),
        UniqueConstraint("document_id", "idempotency_key", name="uq_document_file_version_retry"),
    )
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(700), nullable=False)
    s3_version_id: Mapped[str | None] = mapped_column(String(256))
    content_type: Mapped[str] = mapped_column(String(160), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uploading")
    analysis_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unscanned")
    analysis_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="upload")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
