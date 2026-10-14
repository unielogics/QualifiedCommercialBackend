"""Application-scoped lender packages; each recipient owns an isolated snapshot."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class LenderOffering(TimestampMixin, Base):
    __tablename__ = "lender_offerings"
    __table_args__ = (
        UniqueConstraint("lender_id", "program_key", "version", name="uq_lender_offering_version"),
        Index("uq_lender_offering_active", "lender_id", "program_key", unique=True,
              postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lender_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="CASCADE"), index=True)
    program_key: Mapped[str] = mapped_column(String(64), nullable=False, default="*", server_default="*")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    checklist_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    source_reference: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    fit_rules: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    official_form_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    official_forms_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class SubmissionBatch(TimestampMixin, Base):
    __tablename__ = "submission_batches"
    __table_args__ = (UniqueConstraint("profile_id", "idempotency_key", name="uq_submission_batch_request"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared", server_default="prepared")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LenderSubmission(TimestampMixin, Base):
    __tablename__ = "lender_submissions"
    __table_args__ = (
        UniqueConstraint("batch_id", "lender_id", name="uq_lender_submission_recipient"),
        Index("ix_lender_submissions_queue", "status", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("submission_batches.id", ondelete="CASCADE"), index=True)
    lender_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="RESTRICT"), index=True)
    offering_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("lender_offerings.id", ondelete="RESTRICT"))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    lender_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared", server_default="prepared")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fit_status: Mapped[str] = mapped_column(String(40), nullable=False)
    warning_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    blockers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    requirements_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    provider_message_id: Mapped[str | None] = mapped_column(String(320), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)


class LenderSubmissionEvent(Base):
    __tablename__ = "lender_submission_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("lender_submissions.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
