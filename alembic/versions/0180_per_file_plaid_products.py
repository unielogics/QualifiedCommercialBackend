"""Add per-file Plaid product policy and Item authorization snapshots.

Revision ID: 0180_per_file_plaid_products
Revises: 0179_industry_and_booking_address
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0180_per_file_plaid_products"
down_revision = "0179_industry_and_booking_address"
branch_labels = None
depends_on = None


def _add_policy(table: str, constraint: str) -> None:
    op.add_column(
        table,
        sa.Column("plaid_assets_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        table,
        sa.Column("plaid_statements_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(table, sa.Column("plaid_policy_updated_at", sa.DateTime(timezone=True)))
    op.add_column(
        table,
        sa.Column(
            "plaid_policy_updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.create_check_constraint(
        constraint,
        table,
        "plaid_assets_enabled OR plaid_statements_enabled",
    )


def _add_item_state(table: str) -> None:
    empty = sa.text("'[]'::jsonb")
    op.add_column(
        table,
        sa.Column("plaid_products", postgresql.JSONB(), nullable=False, server_default=empty),
    )
    op.add_column(
        table,
        sa.Column("plaid_consented_products", postgresql.JSONB(), nullable=False, server_default=empty),
    )
    op.add_column(
        table,
        sa.Column("plaid_billed_products", postgresql.JSONB(), nullable=False, server_default=empty),
    )
    op.add_column(
        table,
        sa.Column(
            "plaid_unavailable_products", postgresql.JSONB(), nullable=False, server_default=empty
        ),
    )
    op.add_column(table, sa.Column("plaid_products_checked_at", sa.DateTime(timezone=True)))
    op.add_column(table, sa.Column("statements_refresh_state", sa.String(16)))
    op.add_column(table, sa.Column("statements_refresh_requested_at", sa.DateTime(timezone=True)))


def upgrade() -> None:
    _add_policy("dos_dealers", "ck_dos_dealers_plaid_product_enabled")
    _add_policy("application_profiles", "ck_application_profiles_plaid_product_enabled")
    _add_item_state("dos_plaid_items")
    _add_item_state("application_plaid_items")
    op.add_column(
        "plaid_asset_reports",
        sa.Column(
            "bucket_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="SET NULL"),
        ),
    )

    assets_only = sa.text("'[\"assets\"]'::jsonb")
    op.add_column(
        "dos_bank_consent",
        sa.Column("product_scope", postgresql.JSONB(), nullable=False, server_default=assets_only),
    )
    op.add_column(
        "application_bank_consents",
        sa.Column("product_scope", postgresql.JSONB(), nullable=False, server_default=assets_only),
    )


def downgrade() -> None:
    op.drop_column("plaid_asset_reports", "bucket_file_id")
    op.drop_column("application_bank_consents", "product_scope")
    op.drop_column("dos_bank_consent", "product_scope")
    for table in ("application_plaid_items", "dos_plaid_items"):
        op.drop_column(table, "statements_refresh_requested_at")
        op.drop_column(table, "statements_refresh_state")
        op.drop_column(table, "plaid_products_checked_at")
        op.drop_column(table, "plaid_unavailable_products")
        op.drop_column(table, "plaid_billed_products")
        op.drop_column(table, "plaid_consented_products")
        op.drop_column(table, "plaid_products")
    op.drop_constraint(
        "ck_application_profiles_plaid_product_enabled",
        "application_profiles",
        type_="check",
    )
    op.drop_constraint(
        "ck_dos_dealers_plaid_product_enabled",
        "dos_dealers",
        type_="check",
    )
    for table in ("application_profiles", "dos_dealers"):
        op.drop_column(table, "plaid_policy_updated_by_user_id")
        op.drop_column(table, "plaid_policy_updated_at")
        op.drop_column(table, "plaid_statements_enabled")
        op.drop_column(table, "plaid_assets_enabled")
