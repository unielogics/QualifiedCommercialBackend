"""Add rate-sheet amount and credit tier bands.

Revision ID: 0081_rate_sheet_amount_credit_tiers
Revises: 0080_rate_sheet_crud_fields
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0081_rate_sheet_amount_credit_tiers"
down_revision = "0080_rate_sheet_crud_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rate_sheet", sa.Column("max_fico", sa.Integer(), nullable=True))
    op.add_column(
        "rate_sheet",
        sa.Column("credit_tier", sa.String(length=80), server_default="Base", nullable=False),
    )
    op.add_column(
        "rate_sheet",
        sa.Column("min_loan_amount", sa.Numeric(14, 2), server_default="0", nullable=False),
    )
    op.add_column(
        "rate_sheet",
        sa.Column("max_loan_amount", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rate_sheet", "max_loan_amount")
    op.drop_column("rate_sheet", "min_loan_amount")
    op.drop_column("rate_sheet", "credit_tier")
    op.drop_column("rate_sheet", "max_fico")
