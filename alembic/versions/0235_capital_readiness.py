"""Canonical Capital Readiness, locale separation, and program commercial terms.

Revision ID: 0235_capital_readiness
Revises: 0234_ach_proof_retention
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0235_capital_readiness"
down_revision = "0234_ach_proof_retention"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("communication_locale", sa.String(8), server_default="en", nullable=False),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "communication_locale_source",
            sa.String(32),
            server_default="system_default",
            nullable=False,
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column("communication_locale_updated_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "application_profiles",
        sa.Column("communication_locale_updated_by_user_id", UUID),
    )
    op.add_column(
        "application_profiles",
        sa.Column("self_reported_readiness_diagnostic", JSONB),
    )
    op.create_foreign_key(
        "fk_application_profiles_communication_locale_user",
        "application_profiles",
        "users",
        ["communication_locale_updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_application_profiles_communication_locale",
        "application_profiles",
        "communication_locale IN ('en','es')",
    )
    op.execute(
        sa.text(
            """
            UPDATE application_profiles AS profile
            SET communication_locale = lower(trim(intake.preferred_language)),
                communication_locale_source = 'contact_default'
            FROM public_underwriting_intakes AS intake
            WHERE profile.intake_id = intake.id
              AND lower(trim(intake.preferred_language)) IN ('en', 'es')
            """
        )
    )
    op.add_column(
        "users", sa.Column("ui_locale", sa.String(8), server_default="en", nullable=False)
    )
    op.create_check_constraint("ck_users_ui_locale", "users", "ui_locale IN ('en','es')")
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("artifact_locale", sa.String(8), server_default="en", nullable=False),
    )
    op.create_check_constraint(
        "ck_dealer_prospect_email_draft_artifact_locale",
        "dealer_prospect_email_drafts",
        "artifact_locale IN ('en','es')",
    )
    op.add_column(
        "message_sends",
        sa.Column("artifact_locale", sa.String(8), server_default="en", nullable=False),
    )
    op.create_check_constraint(
        "ck_message_sends_artifact_locale",
        "message_sends",
        "artifact_locale IN ('en','es')",
    )

    op.create_table(
        "capital_readiness_policy_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("policy_key", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("minimum_coverage_pct", sa.Numeric(5, 2), server_default="60", nullable=False),
        sa.Column("pillar_weights", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("metric_thresholds", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("published_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("policy_key", "version", name="uq_capital_readiness_policy_version"),
        sa.CheckConstraint("status IN ('draft','published','retired')", name="ck_capital_readiness_policy_status"),
        sa.CheckConstraint(
            "minimum_coverage_pct >= 60 AND minimum_coverage_pct <= 100",
            name="ck_capital_readiness_policy_coverage",
        ),
    )
    op.create_index(
        "uq_capital_readiness_policy_published",
        "capital_readiness_policy_versions",
        ["policy_key"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )

    op.create_table(
        "application_financial_periods",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=False),
        sa.Column("accounting_basis", sa.String(16), server_default="unknown", nullable=False),
        sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("months_covered", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("review_status", sa.String(16), server_default="submitted", nullable=False),
        sa.Column("cogs_applicability", sa.String(24), server_default="unknown", nullable=False),
        sa.Column("revenue", sa.Numeric(18, 2)),
        sa.Column("cogs", sa.Numeric(18, 2)),
        sa.Column("gross_profit", sa.Numeric(18, 2)),
        sa.Column("operating_expenses", sa.Numeric(18, 2)),
        sa.Column("operating_income", sa.Numeric(18, 2)),
        sa.Column("net_income", sa.Numeric(18, 2)),
        sa.Column("ebitda", sa.Numeric(18, 2)),
        sa.Column("adjusted_ebitda", sa.Numeric(18, 2)),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("source_file_id", UUID, sa.ForeignKey("bucket_files.id", ondelete="SET NULL")),
        sa.Column("source_analysis_id", UUID, sa.ForeignKey("bucket_file_analyses.id", ondelete="SET NULL")),
        sa.Column("extractor_version", sa.String(80)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("reconciliation_warnings", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("submitted_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("profile_id", "idempotency_key", name="uq_application_financial_period_idempotency"),
        sa.CheckConstraint("accounting_basis IN ('cash','accrual','tax','unknown')", name="ck_application_financial_period_basis"),
        sa.CheckConstraint("source_kind IN ('stated','derived','self_reported','ai_extracted')", name="ck_application_financial_period_source"),
        sa.CheckConstraint("review_status IN ('submitted','confirmed','rejected','superseded')", name="ck_application_financial_period_review_status"),
        sa.CheckConstraint("cogs_applicability IN ('applicable','not_applicable','unknown')", name="ck_application_financial_period_cogs_applicability"),
        sa.CheckConstraint("period_end >= period_start", name="ck_application_financial_period_dates"),
        sa.CheckConstraint("months_covered >= 1 AND months_covered <= 60", name="ck_application_financial_period_months"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_application_financial_period_confidence"),
    )
    op.create_index(
        "ix_application_financial_period_profile_period",
        "application_financial_periods",
        ["profile_id", "period_end", "created_at"],
    )

    op.create_table(
        "application_addback_verifications",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("financial_period_id", UUID, sa.ForeignKey("application_financial_periods.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(24), server_default="candidate", nullable=False),
        sa.Column("requires_cpa_attestation", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("evidence_file_id", UUID, sa.ForeignKey("bucket_files.id", ondelete="SET NULL")),
        sa.Column("evidence_note", sa.Text()),
        sa.Column("cpa_attested_at", sa.DateTime(timezone=True)),
        sa.Column("cpa_attested_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("qc_verified_at", sa.DateTime(timezone=True)),
        sa.Column("qc_verified_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("lender_program_key", sa.String(120)),
        sa.Column("lender_decided_at", sa.DateTime(timezone=True)),
        sa.Column("lender_decided_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("profile_id", "idempotency_key", name="uq_application_addback_idempotency"),
        sa.CheckConstraint(
            "status IN ('candidate','evidence_pending','cpa_attested','qc_verified','lender_accepted','lender_rejected','expired')",
            name="ck_application_addback_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_application_addback_amount"),
    )
    op.create_index(
        "ix_application_addback_profile_status",
        "application_addback_verifications",
        ["profile_id", "status", "created_at"],
    )
    op.create_table(
        "application_capital_readiness_actions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("action_key", UUID, nullable=False),
        sa.Column("profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("phase_key", sa.String(48), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("baseline", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("target", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("due_date", sa.Date()),
        sa.Column("dependencies", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("required_evidence", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("expected_impact", sa.Text()),
        sa.Column("status", sa.String(24), server_default="not_started", nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("updated_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("action_key", "version", name="uq_capital_readiness_action_version"),
        sa.UniqueConstraint("profile_id", "idempotency_key", name="uq_capital_readiness_action_idempotency"),
        sa.CheckConstraint(
            "phase_key IN ('baseline_health_check','financial_restructuring','system_tracking','pre_underwriting','prime_capital')",
            name="ck_capital_readiness_action_phase",
        ),
        sa.CheckConstraint(
            "status IN ('not_started','in_progress','blocked','completed','cancelled')",
            name="ck_capital_readiness_action_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_capital_readiness_action_version"),
    )
    op.create_index(
        "uq_capital_readiness_action_current",
        "application_capital_readiness_actions",
        ["action_key"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ix_capital_readiness_action_profile_phase",
        "application_capital_readiness_actions",
        ["profile_id", "phase_key", "status"],
    )

    op.create_table(
        "application_capital_readiness_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("policy_id", UUID, sa.ForeignKey("capital_readiness_policy_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("policy_key", sa.String(80), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.String(32), server_default="score_v2", nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("communication_locale", sa.String(8), server_default="en", nullable=False),
        sa.Column("review_status", sa.String(24), server_default="provisional", nullable=False),
        sa.Column("score", sa.Numeric(5, 2)),
        sa.Column("band", sa.String(32), nullable=False),
        sa.Column("evidence_coverage_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("confidence_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("pillars", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("metrics", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("strengths", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("blockers", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("phases", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("program_opportunities", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_manifest", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("material_change", JSONB),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("supersedes_snapshot_id", UUID, sa.ForeignKey("application_capital_readiness_snapshots.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("profile_id", "snapshot_version", name="uq_application_capital_readiness_version"),
        sa.UniqueConstraint("profile_id", "idempotency_key", name="uq_application_capital_readiness_idempotency"),
        sa.CheckConstraint("communication_locale IN ('en','es')", name="ck_application_capital_readiness_locale"),
        sa.CheckConstraint("review_status IN ('provisional','awaiting_review','confirmed','revised')", name="ck_application_capital_readiness_review_status"),
        sa.CheckConstraint("band IN ('ready_soon','three_to_six_months','six_to_twelve_months','one_plus_year','insufficient_evidence')", name="ck_application_capital_readiness_band"),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_application_capital_readiness_score"),
        sa.CheckConstraint("evidence_coverage_pct >= 0 AND evidence_coverage_pct <= 100", name="ck_application_capital_readiness_coverage"),
        sa.CheckConstraint("confidence_pct >= 0 AND confidence_pct <= 100", name="ck_application_capital_readiness_confidence"),
    )
    op.create_index(
        "ix_application_capital_readiness_profile_created",
        "application_capital_readiness_snapshots",
        ["profile_id", "created_at"],
    )
    op.create_index(
        "ix_application_capital_readiness_band",
        "application_capital_readiness_snapshots",
        ["band", "created_at"],
    )

    op.create_table(
        "profitability_assessments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("snapshot_id", UUID, sa.ForeignKey("application_capital_readiness_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("financial_period_id", UUID, sa.ForeignKey("application_financial_periods.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("gross_margin_pct", sa.Numeric(9, 4)),
        sa.Column("gross_margin_status", sa.String(24), nullable=False),
        sa.Column("net_margin_pct", sa.Numeric(9, 4)),
        sa.Column("net_margin_status", sa.String(24), nullable=False),
        sa.Column("warnings", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "financial_period_id", name="uq_profitability_assessment_period"),
    )
    op.create_table(
        "capital_readiness_reviews",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("snapshot_id", UUID, sa.ForeignKey("application_capital_readiness_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("reviewed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('confirmed','revised')", name="ck_capital_readiness_review_status"),
    )
    op.create_index(
        "ix_capital_readiness_reviews_snapshot",
        "capital_readiness_reviews",
        ["snapshot_id", "created_at"],
    )

    op.create_table(
        "funding_program_commercial_terms",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("program_id", UUID, sa.ForeignKey("funding_program_catalog.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("minimum_amount", sa.Numeric(16, 2)),
        sa.Column("maximum_amount", sa.Numeric(16, 2)),
        sa.Column("minimum_term_months", sa.Integer()),
        sa.Column("maximum_term_months", sa.Integer()),
        sa.Column("pricing_basis", sa.String(32)),
        sa.Column("minimum_pricing", sa.Numeric(10, 4)),
        sa.Column("maximum_pricing", sa.Numeric(10, 4)),
        sa.Column("qc_fee_cap_percent", sa.Numeric(7, 4)),
        sa.Column("qc_fee_default_percent", sa.Numeric(7, 4)),
        sa.Column("disclosures", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_reference", sa.Text()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("published_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("program_id", "version", name="uq_funding_program_commercial_terms_version"),
        sa.CheckConstraint("status IN ('draft','published','retired')", name="ck_funding_program_commercial_terms_status"),
        sa.CheckConstraint("qc_fee_cap_percent IS NULL OR (qc_fee_cap_percent >= 0 AND qc_fee_cap_percent <= 100)", name="ck_funding_program_commercial_terms_fee_cap"),
        sa.CheckConstraint("qc_fee_default_percent IS NULL OR (qc_fee_default_percent >= 0 AND qc_fee_default_percent <= 100)", name="ck_funding_program_commercial_terms_fee_default"),
        sa.CheckConstraint("qc_fee_cap_percent IS NULL OR qc_fee_default_percent IS NULL OR qc_fee_default_percent <= qc_fee_cap_percent", name="ck_funding_program_commercial_terms_default_below_cap"),
    )
    op.create_index(
        "uq_funding_program_commercial_terms_published",
        "funding_program_commercial_terms",
        ["program_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO capital_readiness_policy_versions (
                id, policy_key, version, status, minimum_coverage_pct,
                pillar_weights, metric_thresholds, published_at
            ) VALUES (
                gen_random_uuid(), 'qc_lending_margin_v1', 1, 'published', 60,
                '{
                    "revenue_earnings": 20,
                    "debt_capital": 25,
                    "liquidity_banking": 20,
                    "bookkeeping_tax": 15,
                    "credit_collateral": 10,
                    "transaction_use": 10
                }'::jsonb,
                '{
                    "gross_margin_pct": {"acceptable": 10, "healthy": 13, "very_strong": 18},
                    "net_margin_pct": {"acceptable": 2, "healthy": 3, "very_strong": 5},
                    "dscr": {"acceptable": 1.0, "healthy": 1.25, "very_strong": 1.5}
                }'::jsonb,
                now()
            )
            """
        )
    )
    disclosures = json.dumps(
        [
            "QC origination/success fee is capped at 3%; borrower pricing and lender fees are separate.",
            "Availability and terms remain subject to lender review, eligibility, underwriting, and documentation.",
        ]
    )
    op.execute(
        sa.text(
            """
            INSERT INTO funding_program_commercial_terms (
                id, created_at, updated_at, program_id, version, status,
                pricing_basis, qc_fee_cap_percent, disclosures,
                source_reference, effective_date, published_at
            )
            SELECT gen_random_uuid(), now(), now(), id, 1, 'published',
                   'file_specific', 3.0000, CAST(:disclosures AS jsonb),
                   'QC approved MCA commercial terms', CURRENT_DATE, now()
            FROM funding_program_catalog
            WHERE program_key IN ('mca_refinance', 'revenue_based_financing')
            """
        ).bindparams(disclosures=disclosures)
    )


def downgrade() -> None:
    op.drop_table("funding_program_commercial_terms")
    op.drop_index("ix_capital_readiness_reviews_snapshot", table_name="capital_readiness_reviews")
    op.drop_table("capital_readiness_reviews")
    op.drop_table("profitability_assessments")
    op.drop_index("ix_application_capital_readiness_band", table_name="application_capital_readiness_snapshots")
    op.drop_index("ix_application_capital_readiness_profile_created", table_name="application_capital_readiness_snapshots")
    op.drop_table("application_capital_readiness_snapshots")
    op.drop_index("ix_capital_readiness_action_profile_phase", table_name="application_capital_readiness_actions")
    op.drop_index("uq_capital_readiness_action_current", table_name="application_capital_readiness_actions")
    op.drop_table("application_capital_readiness_actions")
    op.drop_index("ix_application_addback_profile_status", table_name="application_addback_verifications")
    op.drop_table("application_addback_verifications")
    op.drop_index("ix_application_financial_period_profile_period", table_name="application_financial_periods")
    op.drop_table("application_financial_periods")
    op.drop_index("uq_capital_readiness_policy_published", table_name="capital_readiness_policy_versions")
    op.drop_table("capital_readiness_policy_versions")
    op.drop_constraint("ck_users_ui_locale", "users", type_="check")
    op.drop_column("users", "ui_locale")
    op.drop_constraint(
        "ck_message_sends_artifact_locale", "message_sends", type_="check"
    )
    op.drop_column("message_sends", "artifact_locale")
    op.drop_constraint(
        "ck_dealer_prospect_email_draft_artifact_locale",
        "dealer_prospect_email_drafts",
        type_="check",
    )
    op.drop_column("dealer_prospect_email_drafts", "artifact_locale")
    op.drop_constraint("ck_application_profiles_communication_locale", "application_profiles", type_="check")
    op.drop_constraint("fk_application_profiles_communication_locale_user", "application_profiles", type_="foreignkey")
    op.drop_column("application_profiles", "communication_locale_updated_by_user_id")
    op.drop_column("application_profiles", "communication_locale_updated_at")
    op.drop_column("application_profiles", "communication_locale_source")
    op.drop_column("application_profiles", "communication_locale")
    op.drop_column("application_profiles", "self_reported_readiness_diagnostic")
