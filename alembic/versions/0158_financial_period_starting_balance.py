"""store statement starting balances

Revision ID: 0158_financial_start_balance
Revises: 0157_calendar_rsvp_program
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0158_financial_start_balance"
down_revision = "0157_calendar_rsvp_program"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_financial_periods",
        sa.Column("starting_balance", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dos_financial_periods", "starting_balance")
