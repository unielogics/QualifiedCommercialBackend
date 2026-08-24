"""Plaid environment isolation, update mode, and Asset Reports.

Revision ID: 0145_plaid_lifecycle_assets
Revises: 0144_public_mutual_nda
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0145_plaid_lifecycle_assets"
down_revision = "0144_public_mutual_nda"
branch_labels = None
depends_on = None


def _item_lifecycle_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="sandbox"),
    )
    op.add_column(table, sa.Column("update_mode_reason", sa.String(length=32)))
    op.add_column(
        table,
        sa.Column(
            "update_mode_account_selection",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(table, sa.Column("last_webhook_at", sa.DateTime(timezone=True)))


def upgrade() -> None:
    _item_lifecycle_columns("dos_plaid_items")
    _item_lifecycle_columns("application_plaid_items")

    op.create_table(
        "plaid_asset_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "dealer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
        ),
        sa.Column("asset_report_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("encrypted_asset_report_token", sa.Text()),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text()),
        sa.Column("days_requested", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "source_item_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "(profile_id IS NOT NULL)::int + (dealer_id IS NOT NULL)::int = 1",
            name="ck_plaid_asset_report_one_owner",
        ),
    )
    op.create_index("ix_plaid_asset_reports_profile", "plaid_asset_reports", ["profile_id"])
    op.create_index("ix_plaid_asset_reports_dealer", "plaid_asset_reports", ["dealer_id"])


def downgrade() -> None:
    op.drop_index("ix_plaid_asset_reports_dealer", table_name="plaid_asset_reports")
    op.drop_index("ix_plaid_asset_reports_profile", table_name="plaid_asset_reports")
    op.drop_table("plaid_asset_reports")
    for table in ("application_plaid_items", "dos_plaid_items"):
        op.drop_column(table, "last_webhook_at")
        op.drop_column(table, "update_mode_account_selection")
        op.drop_column(table, "update_mode_reason")
        op.drop_column(table, "environment")
