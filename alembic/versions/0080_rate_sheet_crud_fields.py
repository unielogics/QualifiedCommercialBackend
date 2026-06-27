"""Add editable rate-sheet fields.

Revision ID: 0080_rate_sheet_crud_fields
Revises: 0079_bedrock_ai_usage_default
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0080_rate_sheet_crud_fields"
down_revision = "0079_bedrock_ai_usage_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rate_sheet",
        sa.Column("points", sa.Numeric(5, 2), server_default="0", nullable=False),
    )
    op.add_column(
        "rate_sheet",
        sa.Column("min_fico", sa.Integer(), server_default="680", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("rate_sheet", "min_fico")
    op.drop_column("rate_sheet", "points")
