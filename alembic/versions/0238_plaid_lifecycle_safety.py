"""Durable Plaid report cleanup and payment Item environment boundaries."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0238_plaid_lifecycle_safety"
down_revision = "0237_arf_intake_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plaid_asset_reports", sa.Column("provider_removal_requested_at", sa.DateTime(timezone=True)))
    op.add_column("plaid_asset_reports", sa.Column("provider_removed_at", sa.DateTime(timezone=True)))
    op.add_column("plaid_asset_reports", sa.Column("provider_removal_error", sa.Text()))
    op.add_column("payment_funding_sources", sa.Column("plaid_environment", sa.String(16)))
    # The worker verifies the archived PDF before removing an ingested report.
    # Existing payment sources deliberately remain unclassified until reconnect.
    op.execute("""
        UPDATE plaid_asset_reports SET provider_removal_requested_at = now()
        WHERE encrypted_asset_report_token IS NOT NULL
          AND (ingested_at IS NOT NULL OR status = 'removed' OR EXISTS (
            SELECT 1 FROM dos_plaid_items i
            WHERE i.dealer_id = plaid_asset_reports.dealer_id
              AND i.status IN ('revoked', 'removed')
              AND plaid_asset_reports.source_item_ids ? i.id::text
          ) OR EXISTS (
            SELECT 1 FROM application_plaid_items i
            WHERE i.profile_id = plaid_asset_reports.profile_id
              AND i.status IN ('revoked', 'removed')
              AND plaid_asset_reports.source_item_ids ? i.id::text
          ))
    """)


def downgrade() -> None:
    op.drop_column("payment_funding_sources", "plaid_environment")
    op.drop_column("plaid_asset_reports", "provider_removal_error")
    op.drop_column("plaid_asset_reports", "provider_removed_at")
    op.drop_column("plaid_asset_reports", "provider_removal_requested_at")
