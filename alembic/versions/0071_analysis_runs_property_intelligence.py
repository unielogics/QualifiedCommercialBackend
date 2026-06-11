"""analysis runs and property intelligence.

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "provider_secrets",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("encryption_provider", sa.String(length=24), server_default="fernet", nullable=False),
        sa.Column("kms_key_id", sa.String(length=512), nullable=True),
        sa.Column("updated_by_id", pg.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("key", name="uq_provider_secrets_key"),
    )
    op.create_index("ix_provider_secrets_key", "provider_secrets", ["key"])

    op.create_table(
        "provider_usage_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("request_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ok", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("address_hash", sa.String(length=64), nullable=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("broker_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("client_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("loan_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("metadata_json", pg.JSONB(), nullable=True),
    )
    op.create_index("ix_provider_usage_events_provider", "provider_usage_events", ["provider"])
    op.create_index("ix_provider_usage_events_feature", "provider_usage_events", ["feature"])
    op.create_index("ix_provider_usage_events_address_hash", "provider_usage_events", ["address_hash"])
    op.create_index("ix_provider_usage_events_user_id", "provider_usage_events", ["user_id"])
    op.create_index("ix_provider_usage_events_broker_id", "provider_usage_events", ["broker_id"])
    op.create_index("ix_provider_usage_events_client_id", "provider_usage_events", ["client_id"])
    op.create_index("ix_provider_usage_events_loan_id", "provider_usage_events", ["loan_id"])

    op.create_table(
        "property_intelligence_snapshots",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_id", pg.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("deal_id", pg.UUID(as_uuid=True), sa.ForeignKey("deals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("address_hash", sa.String(length=64), nullable=False),
        sa.Column("source_status", pg.JSONB(), nullable=True),
        sa.Column("address", pg.JSONB(), nullable=False),
        sa.Column("google_place", pg.JSONB(), nullable=True),
        sa.Column("rentcast_property", pg.JSONB(), nullable=True),
        sa.Column("rentcast_value", pg.JSONB(), nullable=True),
        sa.Column("rentcast_rent", pg.JSONB(), nullable=True),
        sa.Column("rentcast_market", pg.JSONB(), nullable=True),
        sa.Column("fema_flood", pg.JSONB(), nullable=True),
    )
    op.create_index("ix_property_intelligence_snapshots_created_by_id", "property_intelligence_snapshots", ["created_by_id"])
    op.create_index("ix_property_intelligence_snapshots_client_id", "property_intelligence_snapshots", ["client_id"])
    op.create_index("ix_property_intelligence_snapshots_loan_id", "property_intelligence_snapshots", ["loan_id"])
    op.create_index("ix_property_intelligence_snapshots_deal_id", "property_intelligence_snapshots", ["deal_id"])
    op.create_index("ix_property_intelligence_snapshots_address_hash", "property_intelligence_snapshots", ["address_hash"])

    op.create_table(
        "analysis_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_id", pg.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("deal_id", pg.UUID(as_uuid=True), sa.ForeignKey("deals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("property_snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("property_intelligence_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prequal_request_id", pg.UUID(as_uuid=True), sa.ForeignKey("prequal_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product", sa.String(length=24), nullable=False),
        sa.Column("tool_source", sa.String(length=32), server_default="deal_analyzer", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("title", sa.String(length=180), server_default="Analysis run", nullable=False),
        sa.Column("target_property_address", sa.Text(), nullable=True),
        sa.Column("inputs", pg.JSONB(), nullable=False),
        sa.Column("calculator_output", pg.JSONB(), nullable=True),
        sa.Column("ai_report", pg.JSONB(), nullable=True),
        sa.Column("sanitized_client_report", pg.JSONB(), nullable=True),
        sa.Column("report_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shared_by_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_analysis_runs_created_by_id", "analysis_runs", ["created_by_id"])
    op.create_index("ix_analysis_runs_client_id", "analysis_runs", ["client_id"])
    op.create_index("ix_analysis_runs_deal_id", "analysis_runs", ["deal_id"])
    op.create_index("ix_analysis_runs_loan_id", "analysis_runs", ["loan_id"])
    op.create_index("ix_analysis_runs_property_snapshot_id", "analysis_runs", ["property_snapshot_id"])
    op.create_index("ix_analysis_runs_prequal_request_id", "analysis_runs", ["prequal_request_id"])
    op.create_index("ix_analysis_runs_product", "analysis_runs", ["product"])
    op.create_index("ix_analysis_runs_status", "analysis_runs", ["status"])

    op.add_column(
        "prequal_requests",
        sa.Column("source_analysis_run_id", pg.UUID(as_uuid=True), sa.ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_prequal_requests_source_analysis_run_id", "prequal_requests", ["source_analysis_run_id"])


def downgrade() -> None:
    op.drop_index("ix_prequal_requests_source_analysis_run_id", table_name="prequal_requests")
    op.drop_column("prequal_requests", "source_analysis_run_id")
    op.drop_index("ix_analysis_runs_status", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_product", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_prequal_request_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_property_snapshot_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_loan_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_deal_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_client_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_created_by_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_index("ix_property_intelligence_snapshots_address_hash", table_name="property_intelligence_snapshots")
    op.drop_index("ix_property_intelligence_snapshots_deal_id", table_name="property_intelligence_snapshots")
    op.drop_index("ix_property_intelligence_snapshots_loan_id", table_name="property_intelligence_snapshots")
    op.drop_index("ix_property_intelligence_snapshots_client_id", table_name="property_intelligence_snapshots")
    op.drop_index("ix_property_intelligence_snapshots_created_by_id", table_name="property_intelligence_snapshots")
    op.drop_table("property_intelligence_snapshots")
    op.drop_index("ix_provider_usage_events_loan_id", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_client_id", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_broker_id", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_user_id", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_address_hash", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_feature", table_name="provider_usage_events")
    op.drop_index("ix_provider_usage_events_provider", table_name="provider_usage_events")
    op.drop_table("provider_usage_events")
    op.drop_index("ix_provider_secrets_key", table_name="provider_secrets")
    op.drop_table("provider_secrets")
