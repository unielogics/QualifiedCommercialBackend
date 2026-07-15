from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


bucket_share_files = Table(
    "bucket_share_files",
    Base.metadata,
    Column("share_id", PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="CASCADE"), primary_key=True),
    Column("file_id", PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


bucket_vendor_access_files = Table(
    "bucket_vendor_access_files",
    Base.metadata,
    Column("vendor_access_id", PG_UUID(as_uuid=True), ForeignKey("bucket_vendor_access.id", ondelete="CASCADE"), primary_key=True),
    Column("file_id", PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class Bucket(TimestampMixin, Base):
    __tablename__ = "buckets"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    bucket_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="collecting_documents")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    requested_documents: Mapped[list[BucketRequestedDocument]] = relationship(
        back_populates="bucket", cascade="all, delete-orphan"
    )
    files: Mapped[list[BucketFile]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    file_annotations: Mapped[list[BucketFileAnnotation]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    upload_links: Mapped[list[BucketUploadLink]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    shares: Mapped[list[BucketShare]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    vendor_access: Mapped[list[BucketVendorAccess]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    notes: Mapped[list[BucketNote]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    activity: Mapped[list[BucketActivityLog]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    ai_reviews: Mapped[list[BucketAIReview]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    ai_messages: Mapped[list[BucketAIMessage]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    ai_action_items: Mapped[list[BucketAIActionItem]] = relationship(back_populates="bucket", cascade="all, delete-orphan")


class BucketDocumentTemplate(TimestampMixin, Base):
    __tablename__ = "bucket_document_templates"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    allow_multiple_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class BucketRequestedDocument(TimestampMixin, Base):
    __tablename__ = "bucket_requested_documents"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_document_templates.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    allow_multiple_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested", server_default="requested")
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    bucket: Mapped[Bucket] = relationship(back_populates="requested_documents")
    template: Mapped[BucketDocumentTemplate | None] = relationship()
    files: Mapped[list[BucketFile]] = relationship(back_populates="requested_document")


class BucketUploadLink(TimestampMixin, Base):
    __tablename__ = "bucket_upload_links"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    recipient_name: Mapped[str] = mapped_column(String(180), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allow_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    allow_multiple_sessions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_use_ai_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_view_ai_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    passcode_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bucket: Mapped[Bucket] = relationship(back_populates="upload_links")


class BucketFile(TimestampMixin, Base):
    __tablename__ = "bucket_files"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"), nullable=True
    )
    upload_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_upload_links.id", ondelete="SET NULL"), nullable=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(700), nullable=False)
    content_type: Mapped[str] = mapped_column(String(160), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_by_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    uploaded_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded", server_default="uploaded")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    delete_storage_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_zip_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    zip_entry_path: Mapped[str | None] = mapped_column(String(700), nullable=True)
    extraction_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extraction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 of the last-fetched S3 object bytes; compared against
    # bucket_file_analyses.content_hash to detect an unchanged file and reuse
    # its cached AI analysis without re-spending tokens.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    bucket: Mapped[Bucket] = relationship(back_populates="files")
    requested_document: Mapped[BucketRequestedDocument | None] = relationship(back_populates="files")
    upload_link: Mapped[BucketUploadLink | None] = relationship()
    parent_zip_file: Mapped[BucketFile | None] = relationship(remote_side=[id], back_populates="extracted_files")
    extracted_files: Mapped[list[BucketFile]] = relationship(back_populates="parent_zip_file")
    shares: Mapped[list[BucketShare]] = relationship(
        secondary=bucket_share_files,
        back_populates="files",
    )
    vendor_access: Mapped[list[BucketVendorAccess]] = relationship(
        secondary=bucket_vendor_access_files,
        back_populates="files",
    )
    annotations: Mapped[list[BucketFileAnnotation]] = relationship(back_populates="file", cascade="all, delete-orphan")
    analyses: Mapped[list[BucketFileAnalysis]] = relationship(back_populates="file", cascade="all, delete-orphan")


class BucketFileAnnotation(TimestampMixin, Base):
    __tablename__ = "bucket_file_annotations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vendor_access_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_vendor_access.id", ondelete="SET NULL"), nullable=True, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[str] = mapped_column(String(180), nullable=False)
    author_role: Mapped[str] = mapped_column(String(40), nullable=False)

    bucket: Mapped[Bucket] = relationship(back_populates="file_annotations")
    file: Mapped[BucketFile] = relationship(back_populates="annotations")
    share: Mapped[BucketShare | None] = relationship()
    vendor_access: Mapped[BucketVendorAccess | None] = relationship()


class BucketShare(TimestampMixin, Base):
    __tablename__ = "bucket_shares"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    recipient_name: Mapped[str] = mapped_column(String(180), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    passcode_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    can_preview: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_download: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_add_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_see_internal_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_use_ai_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_view_ai_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_view_ai_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_propose_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    bucket: Mapped[Bucket] = relationship(back_populates="shares")
    files: Mapped[list[BucketFile]] = relationship(
        secondary=bucket_share_files,
        back_populates="shares",
    )


class BucketVendorAccess(TimestampMixin, Base):
    __tablename__ = "bucket_vendor_access"
    __table_args__ = (
        UniqueConstraint("bucket_id", "vendor_user_id", name="uq_bucket_vendor_access_bucket_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    file_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="all_active", server_default="all_active")
    can_preview: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_download: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_add_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_see_internal_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    can_use_ai_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_view_ai_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_view_ai_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    can_propose_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    bucket: Mapped[Bucket] = relationship(back_populates="vendor_access")
    vendor: Mapped[User] = relationship(foreign_keys=[vendor_user_id])
    files: Mapped[list[BucketFile]] = relationship(
        secondary=bucket_vendor_access_files,
        back_populates="vendor_access",
    )


class BucketNote(TimestampMixin, Base):
    __tablename__ = "bucket_notes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True
    )
    vendor_access_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_vendor_access.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(180), nullable=False)
    author_role: Mapped[str] = mapped_column(String(40), nullable=False)
    visibility: Mapped[str] = mapped_column(String(24), nullable=False, default="admin", server_default="admin")
    content: Mapped[str] = mapped_column(Text, nullable=False)

    bucket: Mapped[Bucket] = relationship(back_populates="notes")
    share: Mapped[BucketShare | None] = relationship()
    vendor_access: Mapped[BucketVendorAccess | None] = relationship()


class BucketActivityLog(Base):
    __tablename__ = "bucket_activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    bucket: Mapped[Bucket] = relationship(back_populates="activity")
    actor_user: Mapped[User | None] = relationship(foreign_keys=[actor_user_id])


class BucketAIReview(Base):
    __tablename__ = "bucket_ai_reviews"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", server_default="queued")
    context_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    file_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    bucket: Mapped[Bucket] = relationship(back_populates="ai_reviews")
    requested_by: Mapped[User | None] = relationship(foreign_keys=[requested_by_user_id])


class BucketFileAnalysis(TimestampMixin, Base):
    """Durable, reusable per-file AI analysis. One row per
    (file, content_hash, analysis_version) so a file's bytes are sent to the
    model at most ONCE per version — every surface (buckets, leads, admin, chat,
    whole-bucket review) reads this instead of re-analyzing the file."""

    __tablename__ = "bucket_file_analyses"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_file_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized so all per-file analyses for a bucket are queryable without
    # joining through bucket_files.
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    skip_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    skip_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(48), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    file: Mapped[BucketFile] = relationship(back_populates="analyses")
    bucket: Mapped[Bucket] = relationship()

    __table_args__ = (
        UniqueConstraint("bucket_file_id", "content_hash", "analysis_version", name="uq_file_analysis_hash_version"),
    )


class BucketAIMessage(Base):
    __tablename__ = "bucket_ai_messages"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    upload_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_upload_links.id", ondelete="SET NULL"), nullable=True, index=True
    )
    share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vendor_access_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_vendor_access.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    audience: Mapped[str] = mapped_column(String(24), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_context_patch: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    bucket: Mapped[Bucket] = relationship(back_populates="ai_messages")
    upload_link: Mapped[BucketUploadLink | None] = relationship()
    share: Mapped[BucketShare | None] = relationship()
    vendor_access: Mapped[BucketVendorAccess | None] = relationship()
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])


class BucketAIActionItem(Base):
    __tablename__ = "bucket_ai_action_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_ai_messages.id", ondelete="SET NULL"), nullable=True
    )
    upload_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_upload_links.id", ondelete="SET NULL"), nullable=True, index=True
    )
    share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vendor_access_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_vendor_access.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requested_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed", server_default="proposed")
    route: Mapped[str] = mapped_column(String(24), nullable=False, default="admin", server_default="admin")
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(24), nullable=False, default="ai", server_default="ai")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    bucket: Mapped[Bucket] = relationship(back_populates="ai_action_items")
    source_message: Mapped[BucketAIMessage | None] = relationship()
    upload_link: Mapped[BucketUploadLink | None] = relationship()
    share: Mapped[BucketShare | None] = relationship()
    vendor_access: Mapped[BucketVendorAccess | None] = relationship()
    file: Mapped[BucketFile | None] = relationship()
    requested_document: Mapped[BucketRequestedDocument | None] = relationship()
    created_by_user: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
    approved_by_user: Mapped[User | None] = relationship(foreign_keys=[approved_by_user_id])
