"""Dealer Capital OS core schema — all dos_* tables (isolated product).

Revision ID: 0108_dos_core
Revises: 0107_dealer_lead_channel_seen
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0108_dos_core"
down_revision = "0107_dealer_lead_channel_seen"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _dealer_fk(nullable=False):
    return sa.Column(
        "dealer_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=nullable
    )


def upgrade() -> None:
    op.create_table(
        "dos_dealers",
        _id(),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("legal_name", sa.String(180)),
        sa.Column("ein", sa.String(24)),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(48)),
        sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(8)),
        sa.Column("industry", sa.String(48), nullable=False, server_default="auto_dealer"),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("owner_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("dealer_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("notes", sa.Text()),
        *_ts(),
    )
    op.create_table(
        "dos_financial_periods",
        _id(),
        _dealer_fk(),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("revenue", sa.Numeric(14, 2)),
        sa.Column("net_income", sa.Numeric(14, 2)),
        sa.Column("ebitda_reported", sa.Numeric(14, 2)),
        sa.Column("ebitda_adjusted", sa.Numeric(14, 2)),
        sa.Column("ebitda_bankable", sa.Numeric(14, 2)),
        sa.Column("debt_service", sa.Numeric(14, 2)),
        sa.Column("deposits", sa.Numeric(14, 2)),
        sa.Column("withdrawals", sa.Numeric(14, 2)),
        sa.Column("ending_balance", sa.Numeric(14, 2)),
        sa.Column("low_balance", sa.Numeric(14, 2)),
        sa.Column("avg_daily_balance", sa.Numeric(14, 2)),
        sa.Column("nsf_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("liquidity", pg.JSONB()),
        sa.Column("source", sa.String(24), nullable=False, server_default="upload"),
        sa.Column("reconciled", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("dealer_id", "period", name="uq_dos_period"),
        *_ts(),
    )
    op.create_table(
        "dos_cash_events",
        _id(),
        _dealer_fk(),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("description", sa.String(320), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("category", sa.String(48), nullable=False, server_default="uncategorized"),
        sa.Column("flags", pg.JSONB()),
        sa.Column("invoice_date", sa.Date()),
        sa.Column("due_date", sa.Date()),
        sa.Column("categorized_by", sa.String(24)),
        sa.Column("source", sa.String(24), nullable=False, server_default="upload"),
        *_ts(),
    )
    op.create_index("ix_dos_cash_events_dealer_period", "dos_cash_events", ["dealer_id", "period"])
    op.create_table(
        "dos_addbacks",
        _id(),
        _dealer_fk(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("monthly_amount", sa.Numeric(14, 2)),
        sa.Column("annual_amount", sa.Numeric(14, 2)),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("evidence", sa.Text()),
        sa.Column("source_event_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_cash_events.id", ondelete="SET NULL")),
        sa.Column("decided_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_ts(),
    )
    op.create_table(
        "dos_metric_targets",
        _id(),
        _dealer_fk(),
        sa.Column("metric_key", sa.String(48), nullable=False),
        sa.Column("ai_proposed_value", sa.Numeric(14, 2)),
        sa.Column("ai_rationale", sa.Text()),
        sa.Column("ai_proposed_at", sa.DateTime(timezone=True)),
        sa.Column("admin_value", sa.Numeric(14, 2)),
        sa.Column("admin_set_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("admin_set_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="ai_proposed"),
        sa.UniqueConstraint("dealer_id", "metric_key", name="uq_dos_target"),
        *_ts(),
    )
    op.create_table(
        "dos_metric_snapshots",
        _id(),
        _dealer_fk(),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("metrics", pg.JSONB(), nullable=False),
        sa.Column("score", sa.Numeric(5, 1)),
        sa.Column("tier", sa.String(24)),
        *_ts(),
    )
    op.create_index("ix_dos_snapshots_dealer_asof", "dos_metric_snapshots", ["dealer_id", "as_of"])
    op.create_table(
        "dos_metric_lineage",
        _id(),
        sa.Column(
            "snapshot_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_metric_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_key", sa.String(48), nullable=False),
        sa.Column("ref_kind", sa.String(24), nullable=False),
        sa.Column("ref_id", pg.UUID(as_uuid=True)),
        sa.Column("period", sa.Date()),
    )
    op.create_table(
        "dos_plan_actions",
        _id(),
        _dealer_fk(),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("category", sa.String(24), nullable=False, server_default="liquidity"),
        sa.Column("owner", sa.String(80)),
        sa.Column("timeline", sa.String(80)),
        sa.Column("due_on", sa.Date()),
        sa.Column("status", sa.String(16), nullable=False, server_default="todo"),
        sa.Column("expected_effect", sa.String(120)),
        sa.Column("published", sa.Boolean(), nullable=False, server_default="false"),
        *_ts(),
    )
    op.create_table(
        "dos_alerts",
        _id(),
        _dealer_fk(),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(12), nullable=False, server_default="warn"),
        sa.Column("message", sa.String(320), nullable=False),
        sa.Column("ref_kind", sa.String(24)),
        sa.Column("ref_id", pg.UUID(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        *_ts(),
    )
    op.create_table(
        "dos_credit_profiles",
        _id(),
        _dealer_fk(),
        sa.Column("business_history", pg.JSONB()),
        sa.Column("personal_score", sa.Integer()),
        sa.Column("personal_tier", sa.String(12)),
        sa.UniqueConstraint("dealer_id", name="uq_dos_credit_dealer"),
        *_ts(),
    )
    op.create_table(
        "dos_tax_filings",
        _id(),
        _dealer_fk(),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("filed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("revenue_reported", sa.Numeric(14, 2)),
        sa.Column("deposits_observed", sa.Numeric(14, 2)),
        sa.Column("discrepancy", sa.Text()),
        sa.UniqueConstraint("dealer_id", "year", name="uq_dos_tax_year"),
        *_ts(),
    )
    op.create_table(
        "dos_source_connections",
        _id(),
        _dealer_fk(),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("encrypted_tokens", sa.Text()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dealer_id", "kind", name="uq_dos_source"),
        *_ts(),
    )


def downgrade() -> None:
    for t in [
        "dos_source_connections",
        "dos_tax_filings",
        "dos_credit_profiles",
        "dos_alerts",
        "dos_plan_actions",
        "dos_metric_lineage",
        "dos_metric_snapshots",
        "dos_metric_targets",
        "dos_addbacks",
        "dos_cash_events",
        "dos_financial_periods",
        "dos_dealers",
    ]:
        op.drop_table(t)
