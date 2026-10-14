"""ARF launch permissions and immutable, explainable application assessments."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
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


class ARFChannelConfiguration(TimestampMixin, Base):
    __tablename__ = "arf_channel_configurations"
    __table_args__ = (
        CheckConstraint("intake_mode IN ('referral_only','partner_hosted_authorized')", name="ck_arf_intake_mode"),
        CheckConstraint("publication_state IN ('draft_pending_approval','published','paused')", name="ck_arf_publication_state"),
    )
    channel_key: Mapped[str] = mapped_column(String(32), primary_key=True, default="arf_external")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    intake_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="referral_only", server_default="referral_only")
    publication_state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft_pending_approval", server_default="draft_pending_approval")
    # Independent approvals, each with a written evidence reference and scope.
    approvals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    permitted_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    permitted_documents: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class ApplicationAssessmentSnapshot(TimestampMixin, Base):
    __tablename__ = "application_assessment_snapshots"
    __table_args__ = (Index("ix_application_assessment_profile_version", "profile_id", "version"), UniqueConstraint("profile_id", "version", name="uq_application_assessment_profile_version"))
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(48), nullable=False, default="arf-source-rules-v1")
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    document_usability: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    file_completeness: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    products: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    lender_fit: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    facts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    risk_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    strengths: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    exceptions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    explanation: Mapped[str | None] = mapped_column(Text)


class ARFReferral(TimestampMixin, Base):
    """Minimal contact handoff; contains no financial questionnaire or documents."""
    __tablename__ = "arf_referrals"
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    business_name: Mapped[str | None] = mapped_column(String(180))
    program_key: Mapped[str | None] = mapped_column(String(64))
    industry_key: Mapped[str | None] = mapped_column(String(32))
    attribution: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    consent_version: Mapped[str] = mapped_column(String(48), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
