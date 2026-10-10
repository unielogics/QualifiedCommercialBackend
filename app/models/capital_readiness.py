from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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


class CapitalReadinessPolicyVersion(TimestampMixin, Base):
    """Immutable calculation policy. Only one version per key may be published."""

    __tablename__ = "capital_readiness_policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_key", "version", name="uq_capital_readiness_policy_version"),
        Index(
            "uq_capital_readiness_policy_published",
            "policy_key",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_capital_readiness_policy_status",
        ),
        CheckConstraint(
            "minimum_coverage_pct >= 60 AND minimum_coverage_pct <= 100",
            name="ck_capital_readiness_policy_coverage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_key: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    minimum_coverage_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=60, server_default="60"
    )
    pillar_weights: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    metric_thresholds: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ApplicationFinancialPeriod(TimestampMixin, Base):
    """One immutable, entity-and-period-consistent operating statement observation."""

    __tablename__ = "application_financial_periods"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "idempotency_key", name="uq_application_financial_period_idempotency"
        ),
        Index(
            "ix_application_financial_period_profile_period",
            "profile_id",
            "period_end",
            "created_at",
        ),
        CheckConstraint(
            "accounting_basis IN ('cash','accrual','tax','unknown')",
            name="ck_application_financial_period_basis",
        ),
        CheckConstraint(
            "source_kind IN ('stated','derived','self_reported','ai_extracted')",
            name="ck_application_financial_period_source",
        ),
        CheckConstraint(
            "review_status IN ('submitted','confirmed','rejected','superseded')",
            name="ck_application_financial_period_review_status",
        ),
        CheckConstraint(
            "cogs_applicability IN ('applicable','not_applicable','unknown')",
            name="ck_application_financial_period_cogs_applicability",
        ),
        CheckConstraint(
            "period_end >= period_start",
            name="ck_application_financial_period_dates",
        ),
        CheckConstraint(
            "months_covered >= 1 AND months_covered <= 60",
            name="ck_application_financial_period_months",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_application_financial_period_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_name: Mapped[str] = mapped_column(String(200), nullable=False)
    accounting_basis: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    months_covered: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="submitted", server_default="submitted"
    )
    cogs_applicability: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unknown", server_default="unknown"
    )
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    cogs: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    operating_expenses: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    operating_income: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ebitda: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    adjusted_ebitda: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    source_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_file_analyses.id", ondelete="SET NULL")
    )
    extractor_version: Mapped[str | None] = mapped_column(String(80))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reconciliation_warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ApplicationCapitalReadinessSnapshot(TimestampMixin, Base):
    """Append-only readiness result. JSON fields snapshot exact client-visible output."""

    __tablename__ = "application_capital_readiness_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "snapshot_version", name="uq_application_capital_readiness_version"
        ),
        UniqueConstraint(
            "profile_id", "idempotency_key", name="uq_application_capital_readiness_idempotency"
        ),
        Index(
            "ix_application_capital_readiness_profile_created",
            "profile_id",
            "created_at",
        ),
        Index("ix_application_capital_readiness_band", "band", "created_at"),
        CheckConstraint(
            "review_status IN ('provisional','awaiting_review','confirmed','revised')",
            name="ck_application_capital_readiness_review_status",
        ),
        CheckConstraint(
            "band IN ('ready_soon','three_to_six_months','six_to_twelve_months',"
            "'one_plus_year','insufficient_evidence')",
            name="ck_application_capital_readiness_band",
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="ck_application_capital_readiness_score",
        ),
        CheckConstraint(
            "evidence_coverage_pct >= 0 AND evidence_coverage_pct <= 100",
            name="ck_application_capital_readiness_coverage",
        ),
        CheckConstraint(
            "confidence_pct >= 0 AND confidence_pct <= 100",
            name="ck_application_capital_readiness_confidence",
        ),
        CheckConstraint(
            "communication_locale IN ('en','es')",
            name="ck_application_capital_readiness_locale",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("capital_readiness_policy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_key: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Formula lineage is intentionally separate from the margin/threshold
    # policy so a policy edit cannot silently change the scoring algorithm.
    formula_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="score_v2", server_default="score_v2"
    )
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    communication_locale: Mapped[str] = mapped_column(
        String(8), nullable=False, default="en", server_default="en"
    )
    review_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="provisional", server_default="provisional"
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    band: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_coverage_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confidence_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    pillars: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    metrics: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    strengths: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    blockers: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    phases: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    program_opportunities: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_manifest: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    material_change: Mapped[dict | None] = mapped_column(JSONB)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    supersedes_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_capital_readiness_snapshots.id", ondelete="SET NULL"),
    )


class ProfitabilityAssessment(TimestampMixin, Base):
    __tablename__ = "profitability_assessments"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "financial_period_id", name="uq_profitability_assessment_period"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_capital_readiness_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    financial_period_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_financial_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gross_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    gross_margin_status: Mapped[str] = mapped_column(String(24), nullable=False)
    net_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    net_margin_status: Mapped[str] = mapped_column(String(24), nullable=False)
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )


class ApplicationAddBackVerification(TimestampMixin, Base):
    """Append-safe verification chain for one proposed EBITDA add-back.

    This record never changes filed income.  It only becomes eligible for the
    internal adjusted-EBITDA view after QC verification; lender-specific use is
    separately explicit in the terminal lender decision states.
    """

    __tablename__ = "application_addback_verifications"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "idempotency_key", name="uq_application_addback_idempotency"
        ),
        Index(
            "ix_application_addback_profile_status",
            "profile_id",
            "status",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('candidate','evidence_pending','cpa_attested','qc_verified',"
            "'lender_accepted','lender_rejected','expired')",
            name="ck_application_addback_status",
        ),
        CheckConstraint("amount > 0", name="ck_application_addback_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    financial_period_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_financial_periods.id", ondelete="SET NULL"),
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="candidate", server_default="candidate"
    )
    requires_cpa_attestation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    evidence_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    evidence_note: Mapped[str | None] = mapped_column(Text)
    cpa_attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cpa_attested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    qc_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qc_verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    lender_program_key: Mapped[str | None] = mapped_column(String(120))
    lender_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lender_decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class ApplicationCapitalReadinessAction(TimestampMixin, Base):
    """Versioned roadmap action; prior versions remain audit evidence."""

    __tablename__ = "application_capital_readiness_actions"
    __table_args__ = (
        UniqueConstraint(
            "action_key", "version", name="uq_capital_readiness_action_version"
        ),
        UniqueConstraint(
            "profile_id", "idempotency_key", name="uq_capital_readiness_action_idempotency"
        ),
        Index(
            "uq_capital_readiness_action_current",
            "action_key",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_capital_readiness_action_profile_phase",
            "profile_id",
            "phase_key",
            "status",
        ),
        CheckConstraint(
            "phase_key IN ('baseline_health_check','financial_restructuring',"
            "'system_tracking','pre_underwriting','prime_capital')",
            name="ck_capital_readiness_action_phase",
        ),
        CheckConstraint(
            "status IN ('not_started','in_progress','blocked','completed','cancelled')",
            name="ck_capital_readiness_action_status",
        ),
        CheckConstraint("version >= 1", name="ck_capital_readiness_action_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action_key: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    phase_key: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    baseline: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    target: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    dependencies: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required_evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    expected_impact: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_started", server_default="not_started"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class CapitalReadinessReview(TimestampMixin, Base):
    __tablename__ = "capital_readiness_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed','revised')",
            name="ck_capital_readiness_review_status",
        ),
        Index("ix_capital_readiness_reviews_snapshot", "snapshot_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_capital_readiness_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
