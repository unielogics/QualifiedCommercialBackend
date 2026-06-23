from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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


class Bucket(TimestampMixin, Base):
    __tablename__ = "buckets"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    bucket_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    notes: Mapped[list[BucketNote]] = relationship(back_populates="bucket", cascade="all, delete-orphan")
    activity: Mapped[list[BucketActivityLog]] = relationship(back_populates="bucket", cascade="all, delete-orphan")


class BucketDocumentTemplate(TimestampMixin, Base):
    __tablename__ = "bucket_document_templates"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
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

    bucket: Mapped[Bucket] = relationship(back_populates="files")
    requested_document: Mapped[BucketRequestedDocument | None] = relationship(back_populates="files")
    upload_link: Mapped[BucketUploadLink | None] = relationship()
    shares: Mapped[list[BucketShare]] = relationship(
        secondary=bucket_share_files,
        back_populates="files",
    )
    annotations: Mapped[list[BucketFileAnnotation]] = relationship(back_populates="file", cascade="all, delete-orphan")


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


class BucketNote(TimestampMixin, Base):
    __tablename__ = "bucket_notes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(180), nullable=False)
    author_role: Mapped[str] = mapped_column(String(40), nullable=False)
    visibility: Mapped[str] = mapped_column(String(24), nullable=False, default="admin", server_default="admin")
    content: Mapped[str] = mapped_column(Text, nullable=False)

    bucket: Mapped[Bucket] = relationship(back_populates="notes")
    share: Mapped[BucketShare | None] = relationship()


class BucketActivityLog(Base):
    __tablename__ = "bucket_activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    bucket: Mapped[Bucket] = relationship(back_populates="activity")
