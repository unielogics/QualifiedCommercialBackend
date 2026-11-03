"""Which checking accounts count on a file.

A business often banks at more than one account. The AI detects each
account on the statements; the desk chooses which ones count toward the
statement-months requirement and which the financial cockpit reads by
default. Empty means every account counts.

Revision ID: 0248_counted_bank_accounts
Revises: 0247_cp_listing_products
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0248_counted_bank_accounts"
down_revision = "0247_cp_listing_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("counted_bank_accounts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "counted_bank_accounts")
