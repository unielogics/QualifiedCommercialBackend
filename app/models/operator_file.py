from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import relationship as orm_relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class BucketIntakeLink(TimestampMixin, Base):
    """Durable, reversible access from an AI intake to an external bucket."""

    __tablename__ = "bucket_intake_links"
    __table_args__ = (
        UniqueConstraint("bucket_id", "intake_id", name="uq_bucket_intake_links_pair"),
        Index("ix_bucket_intake_links_bucket_active", "bucket_id", "unlinked_at"),
        Index("ix_bucket_intake_links_intake_active", "intake_id", "unlinked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("buckets.id", ondelete="CASCADE"),
        nullable=False,
    )
    intake_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship: Mapped[str] = mapped_column(
        String(24), nullable=False, default="supporting", server_default="supporting"
    )
    note: Mapped[str | None] = mapped_column(Text)
    linked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    unlinked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    files: Mapped[list[BucketIntakeLinkFile]] = orm_relationship(
        back_populates="link", cascade="all, delete-orphan"
    )


class BucketIntakeLinkFile(TimestampMixin, Base):
    """A selected BucketFile reference; the underlying S3 object is never copied."""

    __tablename__ = "bucket_intake_link_files"
    __table_args__ = (
        UniqueConstraint("link_id", "bucket_file_id", name="uq_bucket_intake_link_files_pair"),
        Index("ix_bucket_intake_link_files_active", "link_id", "removed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_intake_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket_file_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    selected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    removed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    link: Mapped[BucketIntakeLink] = orm_relationship(back_populates="files")
