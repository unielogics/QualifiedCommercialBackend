"""Closing-cost tiers — SUPER_ADMIN-configurable global table.

Loan-amount tiers used by the Deal Analyzer to derive the closing
percentage. Seeded with sensible defaults so the analyzer works
immediately after deploy.

Revision ID: 0058
Revises: 0057
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "closing_cost_tiers",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("from_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("to_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("percentage", sa.Numeric(6, 4), nullable=False, server_default="0"),
        sa.Column(
            "minimum_dollar", sa.Numeric(12, 2), nullable=False, server_default="0"
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.bulk_insert(
        sa.table(
            "closing_cost_tiers",
            sa.column("from_amount", sa.Numeric(14, 2)),
            sa.column("to_amount", sa.Numeric(14, 2)),
            sa.column("percentage", sa.Numeric(6, 4)),
            sa.column("minimum_dollar", sa.Numeric(12, 2)),
            sa.column("sort_order", sa.Integer()),
        ),
        [
            {"from_amount": 0, "to_amount": 100000, "percentage": 0.02, "minimum_dollar": 4000, "sort_order": 0},
            {"from_amount": 100000, "to_amount": 500000, "percentage": 0.02, "minimum_dollar": 3000, "sort_order": 1},
            {"from_amount": 500000, "to_amount": None, "percentage": 0.015, "minimum_dollar": 0, "sort_order": 2},
        ],
    )


def downgrade() -> None:
    op.drop_table("closing_cost_tiers")
