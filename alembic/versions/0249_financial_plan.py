"""The financial plan on a file.

Which obligations the new loan pays off, which statement payees are not
operating spend, and the payment the new loan adds. One record the DSCR, the
simulator, the debt schedule and the marketplace offer all read, so an
action in one place recalculates everywhere.

Revision ID: 0249_financial_plan
Revises: 0248_counted_bank_accounts
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0249_financial_plan"
down_revision = "0248_counted_bank_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("financial_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "financial_plan")
