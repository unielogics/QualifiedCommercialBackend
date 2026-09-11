from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
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


class FundingProgramCatalog(TimestampMixin, Base):
    """Stable public identity for a funding product."""

    __tablename__ = "funding_program_catalog"
    __table_args__ = (
        UniqueConstraint("program_key", name="uq_funding_program_catalog_key"),
        UniqueConstraint("public_slug", name="uq_funding_program_catalog_slug"),
        Index("ix_funding_program_catalog_status_order", "status", "display_order"),
        CheckConstraint(
            "status IN ('active','retired')",
            name="ck_funding_program_catalog_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    program_key: Mapped[str] = mapped_column(String(64), nullable=False)
    public_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class FundingProgramScope(TimestampMixin, Base):
    """A hard catalog boundary evaluated before underwriting fit rules."""

    __tablename__ = "funding_program_scopes"
    __table_args__ = (
        UniqueConstraint("program_id", "vertical", "scope_key", name="uq_funding_program_scope"),
        Index("ix_funding_program_scopes_vertical", "vertical", "program_id"),
        CheckConstraint(
            "vertical IN ('real_estate','dealer','main_street','mca')",
            name="ck_funding_program_scope_vertical",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("funding_program_catalog.id", ondelete="CASCADE"),
        nullable=False,
    )
    vertical: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(
        String(80), nullable=False, default="default", server_default="default"
    )
    intake_variants: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    intent_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    naics_prefixes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    industry_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required_fact_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class ApplicationEvidencePolicySelection(TimestampMixin, Base):
    """Pinned checklist policy, intentionally independent from loan products."""

    __tablename__ = "application_evidence_policy_selections"
    __table_args__ = (
        Index(
            "uq_application_evidence_policy_active",
            "profile_id",
            "policy_key",
            unique=True,
            postgresql_where=text("replaced_at IS NULL"),
        ),
        Index("ix_application_evidence_policy_profile", "profile_id", "selected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_playbook_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    playbook_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_name: Mapped[str] = mapped_column(String(160), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationRequirementEvidenceDecision(TimestampMixin, Base):
    """Append-only decision for one requirement/evidence analysis tuple."""

    __tablename__ = "application_requirement_evidence_decisions"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_application_requirement_evidence_decision_idempotency",
        ),
        Index(
            "ix_application_requirement_evidence_decision_evidence",
            "requirement_evidence_id",
            "created_at",
        ),
        Index("ix_application_requirement_evidence_decision_analysis", "analysis_id"),
        CheckConstraint(
            "decision IN ('processing','accepted','needs_more','rejected','failed')",
            name="ck_application_requirement_evidence_decision",
        ),
        CheckConstraint(
            "actor_kind IN ('ai','staff','system')",
            name="ck_application_requirement_evidence_decision_actor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    requirement_evidence_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_requirement_evidence_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_file_analyses.id", ondelete="SET NULL"),
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(16))
    actor_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ai", server_default="ai"
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_requirement_evidence_decisions.id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
