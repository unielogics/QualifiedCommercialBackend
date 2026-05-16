"""Closing-cost tiers — two-fee split (with vs without construction).

Adds `percentage_no_construction` (the closing % when the borrower
self-funds construction) and drops the now-unused `minimum_dollar`
floor. Existing rows backfill `percentage_no_construction = percentage`
so behavior is unchanged until a super-admin edits the table.

Revision ID: 0059
Revises: 0058
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "closing_cost_tiers",
        sa.Column(
            "percentage_no_construction",
            sa.Numeric(6, 4),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        "UPDATE closing_cost_tiers SET percentage_no_construction = percentage"
    )
    op.drop_column("closing_cost_tiers", "minimum_dollar")


def downgrade() -> None:
    op.add_column(
        "closing_cost_tiers",
        sa.Column(
            "minimum_dollar",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.drop_column("closing_cost_tiers", "percentage_no_construction")
